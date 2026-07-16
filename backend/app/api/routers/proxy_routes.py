from __future__ import annotations

import asyncio
import base64
from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
from io import BytesIO
from pathlib import Path
import secrets
import zipfile

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.api.capacity import notify_capacity_changed, notify_validation_queued
from app.api.schemas import (
    HomeRelayEnrollOut,
    HomeRelayEnrollRequest,
    HomeRelayHostKey,
    HomeRelaySetupOut,
    HomeRelaySetupRequest,
    ProxyRouteCreate,
    ProxyRouteDefaultUpdate,
    ProxyRouteListOut,
    ProxyRouteOut,
    ProxyRouteUpdate,
)
from app.config import get_settings
from app.check_job_queue import CheckJobQueue
from app.models.account import Account, AccountCheckJob
from app.models.audit import AuditLog
from app.models.proxy_route import HomeRelaySetup, ProxyRoute
from app.models.settings import SellerSettings
from app.services.proxy_routes import (
    generate_setup_token,
    install_authorized_key,
    probe_proxy_route,
    proxy_route_check_is_fresh,
    publish_proxy_probe_result,
    read_ssh_host_public_key,
    revoke_authorized_key,
    token_hash,
    validate_public_key,
)


router = APIRouter(
    prefix="/api/proxy-routes",
    tags=["proxy-routes"],
    dependencies=[Depends(get_current_user)],
)
public_router = APIRouter(prefix="/api/proxy-routes", tags=["proxy-routes"])

_INSTALLER_FILES = (
    "Install.ps1",
    "Common.ps1",
    "Relay.ps1",
    "Start.ps1",
    "Stop.ps1",
    "Status.ps1",
    "Uninstall.ps1",
    "README.md",
)
_INSTALLER_ARCHIVE_LOCK = asyncio.Lock()
_INSTALLER_ARCHIVE_BYTES: bytes | None = None
_PUBLIC_RATE_LOCK = asyncio.Lock()
_PUBLIC_RATE_HASH_KEY = secrets.token_bytes(32)
_INSTALLER_REQUESTS: dict[bytes, deque[float]] = {}
_INSTALLER_GLOBAL_REQUESTS: deque[tuple[float, bytes]] = deque()
_ENROLL_REQUESTS: dict[bytes, deque[float]] = {}
_ENROLL_GLOBAL_REQUESTS: deque[tuple[float, bytes]] = deque()
_PUBLIC_RATE_WINDOW_SECONDS = 60.0
_INSTALLER_REQUEST_LIMIT = 120
_INSTALLER_GLOBAL_REQUEST_LIMIT = 12_000
_ENROLL_REQUEST_LIMIT = 30
_ENROLL_GLOBAL_REQUEST_LIMIT = 3_000
_ROUTE_CHANGE_QUEUE = CheckJobQueue()


def _route_out(route: ProxyRoute, default_route_id: int | None) -> ProxyRouteOut:
    presented_status = route.status
    presented_error = route.last_error
    if (
        route.enabled
        and route.status == "online"
        and not proxy_route_check_is_fresh(route.last_checked_at)
    ):
        presented_status = "offline"
        presented_error = "proxy_check_stale"
    return ProxyRouteOut(
        id=route.id,
        name=route.name,
        mode=route.mode,
        proxy_type=route.proxy_type,
        host=route.host,
        port=route.port,
        username=route.username_encrypted,
        has_password=bool(route.password_encrypted),
        enabled=route.enabled,
        config_revision=route.config_revision,
        is_default=route.id == default_route_id,
        status=presented_status,
        egress_ip=route.egress_ip,
        latency_ms=route.latency_ms,
        last_checked_at=route.last_checked_at,
        last_error=presented_error,
        created_at=route.created_at,
        updated_at=route.updated_at,
    )


async def _settings(session: AsyncSession) -> SellerSettings:
    settings = await session.get(SellerSettings, 1)
    if settings is None:
        settings = SellerSettings(id=1)
        session.add(settings)
        await session.flush()
    return settings


async def _settings_for_update(session: AsyncSession) -> SellerSettings:
    settings = (
        await session.execute(
            select(SellerSettings)
            .where(SellerSettings.id == 1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if settings is None:
        settings = SellerSettings(id=1)
        session.add(settings)
        await session.flush()
    return settings


async def _route_or_404(session: AsyncSession, route_id: int) -> ProxyRoute:
    route = await session.get(ProxyRoute, route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="Proxy route not found")
    return route


async def _route_for_update_or_404(
    session: AsyncSession,
    route_id: int,
) -> ProxyRoute:
    route = (
        await session.execute(
            select(ProxyRoute)
            .where(ProxyRoute.id == route_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if route is None:
        raise HTTPException(status_code=404, detail="Proxy route not found")
    return route


async def _lock_home_relay_setups(
    session: AsyncSession,
    route_id: int,
) -> list[HomeRelaySetup]:
    return list(
        (
            await session.execute(
                select(HomeRelaySetup)
                .where(HomeRelaySetup.route_id == route_id)
                .order_by(HomeRelaySetup.id)
                .with_for_update()
            )
        ).scalars()
    )


async def _mark_route_offline_after_repair_failure(
    session: AsyncSession,
    route_id: int,
) -> bool:
    """Best-effort durable fail-closed state after an external key revoke."""

    try:
        persisted = (
            await session.execute(
                select(ProxyRoute)
                .where(ProxyRoute.id == route_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if persisted is not None:
            persisted.enabled = False
            persisted.status = "offline"
            persisted.last_error = "repair_commit_failed"
            persisted.config_revision += 1
            persisted.updated_at = datetime.now(timezone.utc)
        await session.commit()
    except Exception:
        await session.rollback()
        return False
    return True


async def _route_has_account_reference(
    session: AsyncSession,
    route_id: int,
) -> bool:
    account_id = (
        await session.execute(
            select(Account.id).where(Account.proxy_route_id == route_id).limit(1)
        )
    ).scalar_one_or_none()
    return account_id is not None


async def _lock_accounts_for_route_change(
    session: AsyncSession,
    *,
    route_id: int | None = None,
    include_inherited: bool = False,
) -> list[Account]:
    """Lock affected Account -> Job rows and reject live validation races."""

    predicates = []
    if route_id is not None:
        predicates.append(Account.proxy_route_id == route_id)
    if include_inherited:
        predicates.append(Account.proxy_route_id.is_(None))
    if not predicates:
        return []

    condition = predicates[0] if len(predicates) == 1 else or_(*predicates)
    accounts = list(
        (
            await session.execute(
                select(Account)
                .where(condition)
                .order_by(Account.id)
                .with_for_update()
            )
        ).scalars()
    )
    if not accounts:
        return []

    jobs = list(
        (
            await session.execute(
                select(AccountCheckJob)
                .where(
                    AccountCheckJob.account_id.in_(
                        [account.id for account in accounts]
                    ),
                    AccountCheckJob.status.in_(("pending", "running")),
                )
                .order_by(AccountCheckJob.account_id, AccountCheckJob.id)
                .with_for_update()
            )
        ).scalars()
    )
    if jobs:
        first = jobs[0]
        raise HTTPException(
            status_code=409,
            detail=(
                "Proxy route cannot change while account validation "
                f"job {first.id} is {first.status}"
            ),
        )
    return accounts


async def _queue_accounts_after_route_change(
    session: AsyncSession,
    accounts: list[Account],
    *,
    route_id: int | None,
) -> int:
    """Make old validation evidence unsellable and enqueue a fresh pass."""

    for account in accounts:
        account.status = account.operator_status_override or "pending_validation"
        account.chatgpt_last_check_at = None
        account.validation_rerun_requested = False
        job = await _ROUTE_CHANGE_QUEUE.enqueue(
            session,
            account.id,
            priority="manual",
            job_type="full_validation",
        )
        session.add(
            AuditLog(
                event_type="account_proxy_route_invalidated",
                account_id=account.id,
                metadata_={
                    "actor": "admin",
                    "route_id": route_id,
                    "job_id": job.id,
                },
            )
        )
    return len(accounts)


def _home_relay_installer_source() -> Path:
    candidates = (
        Path("/app/ops/windows-relay"),
        Path.cwd() / "ops" / "windows-relay",
        Path(__file__).resolve().parents[4] / "ops" / "windows-relay",
    )
    source = next((candidate for candidate in candidates if candidate.is_dir()), None)
    if source is None or any(
        not (source / name).is_file() for name in _INSTALLER_FILES
    ):
        raise FileNotFoundError("home_relay_installer_unavailable")
    return source


def _build_home_relay_installer_archive() -> bytes:
    """Build byte-for-byte stable ZIP data for command-side verification."""

    source = _home_relay_installer_source()
    archive = BytesIO()
    with zipfile.ZipFile(
        archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as bundle:
        for name in _INSTALLER_FILES:
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            bundle.writestr(entry, (source / name).read_bytes(), compresslevel=9)
    return archive.getvalue()


async def _home_relay_installer_archive() -> bytes:
    """Build the immutable installer once per backend process."""

    global _INSTALLER_ARCHIVE_BYTES
    if _INSTALLER_ARCHIVE_BYTES is not None:
        return _INSTALLER_ARCHIVE_BYTES
    async with _INSTALLER_ARCHIVE_LOCK:
        if _INSTALLER_ARCHIVE_BYTES is None:
            _INSTALLER_ARCHIVE_BYTES = await asyncio.to_thread(
                _build_home_relay_installer_archive
            )
    return _INSTALLER_ARCHIVE_BYTES


def _public_rate_client_key(request: Request) -> bytes:
    """Hash the proxy-normalized peer without retaining its raw address."""

    # Uvicorn updates request.client from forwarded headers only when the
    # direct peer matches --forwarded-allow-ips. Reading X-Forwarded-For here
    # would let a direct client choose its own rate-limit bucket.
    client_host = request.client.host if request.client is not None else ""
    try:
        address = ipaddress.ip_address(client_host)
    except ValueError:
        normalized_host = client_host.strip().casefold() or "unknown"
    else:
        if (
            isinstance(address, ipaddress.IPv6Address)
            and address.ipv4_mapped is not None
        ):
            address = address.ipv4_mapped
        normalized_host = address.compressed
    return hashlib.blake2s(
        normalized_host.encode("utf-8"),
        key=_PUBLIC_RATE_HASH_KEY,
        digest_size=16,
    ).digest()


async def _enforce_public_rate_limit(
    attempts_by_client: dict[bytes, deque[float]],
    global_attempts: deque[tuple[float, bytes]],
    *,
    request: Request,
    per_client_limit: int,
    global_limit: int,
) -> None:
    """Bound public work per client and globally with bounded hashed state."""

    now = asyncio.get_running_loop().time()
    cutoff = now - _PUBLIC_RATE_WINDOW_SECONDS
    client_key = _public_rate_client_key(request)
    async with _PUBLIC_RATE_LOCK:
        while global_attempts and global_attempts[0][0] <= cutoff:
            _expired_at, expired_client_key = global_attempts.popleft()
            expired_client_attempts = attempts_by_client.get(
                expired_client_key
            )
            if expired_client_attempts is None:
                continue
            while (
                expired_client_attempts
                and expired_client_attempts[0] <= cutoff
            ):
                expired_client_attempts.popleft()
            if not expired_client_attempts:
                attempts_by_client.pop(expired_client_key, None)

        client_attempts = attempts_by_client.get(client_key)
        if (
            client_attempts is not None
            and len(client_attempts) >= per_client_limit
        ):
            raise HTTPException(status_code=429, detail="Too many requests")
        if len(global_attempts) >= global_limit:
            raise HTTPException(status_code=429, detail="Too many requests")
        if client_attempts is None:
            client_attempts = deque()
            attempts_by_client[client_key] = client_attempts
        client_attempts.append(now)
        global_attempts.append((now, client_key))


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _build_home_relay_setup_command(
    *,
    script_url: str,
    pairing_url: str,
    pairing_token: str,
    installer_sha256: str,
    autostart: bool,
) -> str:
    """Create a PS5.1-compatible, checksum-pinned installer command."""

    autostart_switch = "-EnableAutoStart" if autostart else "-DisableAutoStart"
    common_body = (
        "$archivePath = Join-Path $stagePath 'installer.zip'; "
        "$extractPath = Join-Path $stagePath 'content'; "
        "$installerPath = Join-Path $extractPath 'Install.ps1'; "
        f"$expectedHash = {_powershell_literal(installer_sha256)}; "
        "try { "
        f"Invoke-WebRequest -UseBasicParsing {_powershell_literal(script_url)} "
        "-OutFile $archivePath; "
        "$actualHash = (Get-FileHash -LiteralPath $archivePath "
        "-Algorithm SHA256).Hash.ToLowerInvariant(); "
        "if ($actualHash -ne $expectedHash) { "
        "throw 'Installer checksum mismatch' }; "
        "Expand-Archive -LiteralPath $archivePath -DestinationPath "
        "$extractPath -Force; & $installerPath "
        f"-PairingUrl {_powershell_literal(pairing_url)} "
        f"-PairingCode {_powershell_literal(pairing_token)} "
        f"{autostart_switch} -CreateDesktopShortcuts; "
        "} finally { Remove-Item -LiteralPath $stagePath -Recurse "
        "-Force -ErrorAction SilentlyContinue }"
    )
    # Directory.CreateDirectory(path, ACL) is atomic on Windows PowerShell
    # 5.1/.NET Framework. It prevents a second local user from taking over the
    # staging path between creation and Set-Acl.
    protected_body = (
        "$ErrorActionPreference = 'Stop'; "
        "$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules'); "
        "$stageAcl = New-Object "
        "Security.AccessControl.DirectorySecurity; "
        "$stageAcl.SetSecurityDescriptorSddlForm("
        "'O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)'); "
        "$commonData = [Environment]::GetFolderPath("
        "[Environment+SpecialFolder]::CommonApplicationData); "
        "$stagePath = Join-Path $commonData "
        "('FunPayHomeRelay-Staging-' + [Guid]::NewGuid().ToString('N')); "
        "$null = [IO.Directory]::CreateDirectory($stagePath, $stageAcl); "
        + common_body
    )
    local_body = (
        "$ErrorActionPreference = 'Stop'; "
        "$localData = [Environment]::GetFolderPath("
        "[Environment+SpecialFolder]::LocalApplicationData); "
        "$stagePath = Join-Path $localData "
        "('FunPayHomeRelay-Staging-' + [Guid]::NewGuid().ToString('N')); "
        "$null = New-Item -ItemType Directory -Path $stagePath; "
        "Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force; "
        + common_body
    )
    encoded_protected = base64.b64encode(
        protected_body.encode("utf-16-le")
    ).decode("ascii")
    administrator_check = (
        "$principal = [Security.Principal.WindowsPrincipal]::new("
        "[Security.Principal.WindowsIdentity]::GetCurrent()); "
        "$isAdministrator = $principal.IsInRole("
        "[Security.Principal.WindowsBuiltInRole]::Administrator); "
    )
    power_shell_51 = (
        "$windowsRoot = [IO.Directory]::GetParent("
        "[Environment]::SystemDirectory).FullName; "
        "$powerShell51 = [IO.Path]::Combine($windowsRoot, "
        "'System32\\WindowsPowerShell\\v1.0\\powershell.exe'); "
        "if (-not [IO.File]::Exists($powerShell51)) { "
        "throw 'Windows PowerShell 5.1 is required' }; "
        "& $powerShell51 -NoLogo -NoProfile -NonInteractive "
        "-ExecutionPolicy Bypass -EncodedCommand "
        f"{_powershell_literal(encoded_protected)}; "
        "if ($LASTEXITCODE -ne 0) { "
        "throw \"Installer failed with exit code $LASTEXITCODE\" }"
    )
    if autostart:
        return (
            administrator_check
            + "if (-not $isAdministrator) { "
            "throw 'Open PowerShell using Run as administrator and retry' }; "
            + power_shell_51
        )
    return (
        administrator_check
        + "if ($isAdministrator) { "
        + power_shell_51
        + " } else { "
        + local_body
        + " }"
    )


@router.get("", response_model=ProxyRouteListOut)
async def list_proxy_routes(
    session: AsyncSession = Depends(get_db_session),
) -> ProxyRouteListOut:
    settings = await session.get(SellerSettings, 1)
    default_id = settings.default_proxy_route_id if settings is not None else None
    routes = (
        await session.execute(select(ProxyRoute).order_by(ProxyRoute.name, ProxyRoute.id))
    ).scalars().all()
    return ProxyRouteListOut(
        default_route_id=default_id,
        routes=[_route_out(route, default_id) for route in routes],
    )


@router.post("", response_model=ProxyRouteOut, status_code=201)
async def create_proxy_route(
    req: ProxyRouteCreate,
    session: AsyncSession = Depends(get_db_session),
) -> ProxyRouteOut:
    if req.is_default:
        raise HTTPException(
            status_code=409,
            detail="Test the proxy route before making it default",
        )
    route = ProxyRoute(
        name=req.name,
        mode=req.mode,
        proxy_type=req.proxy_type,
        host=req.host,
        port=req.port,
        username_encrypted=req.username,
        password_encrypted=req.password,
        enabled=req.enabled,
    )
    session.add(route)
    settings = await _settings(session)
    try:
        await session.flush()
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Proxy route name already exists")
    await session.refresh(route)
    return _route_out(route, settings.default_proxy_route_id)


@router.put("/default", response_model=ProxyRouteListOut)
async def set_default_proxy_route(
    req: ProxyRouteDefaultUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> ProxyRouteListOut:
    current_settings = await _settings(session)
    if current_settings.default_proxy_route_id == req.route_id:
        return await list_proxy_routes(session)

    affected_accounts = await _lock_accounts_for_route_change(
        session,
        include_inherited=True,
    )
    if req.route_id is not None:
        route = await _route_for_update_or_404(session, req.route_id)
        if (
            not route.enabled
            or route.status != "online"
            or not proxy_route_check_is_fresh(route.last_checked_at)
        ):
            raise HTTPException(
                status_code=409,
                detail="Only a tested online route can be default",
            )
    settings = await _settings_for_update(session)
    if settings.default_proxy_route_id == req.route_id:
        return await list_proxy_routes(session)
    settings.default_proxy_route_id = req.route_id
    queued = await _queue_accounts_after_route_change(
        session,
        affected_accounts,
        route_id=req.route_id,
    )
    await session.commit()
    notify_capacity_changed(request)
    if queued:
        notify_validation_queued(request)
    return await list_proxy_routes(session)


@router.post("/home-relay/setup", response_model=HomeRelaySetupOut, status_code=201)
async def setup_home_relay(
    req: HomeRelaySetupRequest,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> HomeRelaySetupOut:
    app_settings = get_settings()
    try:
        archive_bytes = await _home_relay_installer_archive()
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Installer is unavailable") from exc
    installer_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    route = (
        await session.execute(
            select(ProxyRoute)
            .where(ProxyRoute.mode == "home_relay")
            .order_by(ProxyRoute.id)
            .with_for_update()
        )
    ).scalars().first()
    settings = await _settings_for_update(session)
    repair_revoked = False
    repair_route_id: int | None = None
    if route is None:
        route = ProxyRoute(
            name=req.name,
            mode="home_relay",
            proxy_type="socks5",
            host=app_settings.home_relay_proxy_host,
            port=app_settings.home_relay_proxy_port,
            enabled=True,
        )
        session.add(route)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(status_code=409, detail="Proxy route name already exists")
    else:
        # Global order: route -> seller settings -> setup rows. Keep this in
        # sync with default mutation and delete to avoid stale references or
        # deadlocks.
        await _lock_home_relay_setups(session, route.id)
        if (
            settings.default_proxy_route_id == route.id
            or await _route_has_account_reference(session, route.id)
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Unassign the home relay from the default and every "
                    "account before pairing it again"
                ),
            )
        # Flush the unique name mutation while the old key is still valid.
        # A duplicate must fail before the irreversible revoke/kill step.
        route.name = req.name
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=409,
                detail="Proxy route name already exists",
            ) from exc
        repair_route_id = route.id
        repair_revoked = True
        try:
            # Re-pair begins by making every previous PC key unusable.  If the
            # operator abandons the new one-time token, the old relay must not
            # continue indefinitely in the background.
            await revoke_authorized_key(route.id)
        except OSError as exc:
            failed_at = datetime.now(timezone.utc)
            await session.execute(
                update(HomeRelaySetup)
                .where(
                    HomeRelaySetup.route_id == route.id,
                    HomeRelaySetup.consumed_at.is_(None),
                )
                .values(consumed_at=failed_at)
            )
            route.enabled = False
            route.status = "offline"
            route.last_error = "relay_session_ack_timeout"
            route.config_revision += 1
            route.updated_at = failed_at
            try:
                await session.commit()
            except Exception as commit_exc:
                await session.rollback()
                marked = await _mark_route_offline_after_repair_failure(
                    session,
                    repair_route_id,
                )
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Previous relay was revoked but failure state "
                        "could not be committed"
                        if not marked
                        else "Previous relay revoke failed during commit"
                    ),
                ) from commit_exc
            raise HTTPException(
                status_code=503,
                detail="Previous home relay session could not be revoked",
            ) from exc
        route.host = app_settings.home_relay_proxy_host
        route.port = app_settings.home_relay_proxy_port
        route.proxy_type = "socks5"
        route.enabled = True
        route.status = "unchecked"
        route.egress_ip = None
        route.latency_ms = None
        route.last_checked_at = None
        route.last_error = None
        route.config_revision += 1

    now = datetime.now(timezone.utc)
    await session.execute(
        update(HomeRelaySetup)
        .where(
            HomeRelaySetup.route_id == route.id,
            HomeRelaySetup.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    raw_token = generate_setup_token()
    expires_at = now + timedelta(
        seconds=app_settings.home_relay_setup_ttl_seconds
    )
    session.add(
        HomeRelaySetup(
            route_id=route.id,
            token_hash=token_hash(raw_token),
            expires_at=expires_at,
        )
    )
    try:
        await session.commit()
    except Exception as exc:
        await session.rollback()
        marked = True
        if repair_revoked:
            marked = await _mark_route_offline_after_repair_failure(
                session,
                repair_route_id,
            )
        if not marked:
            raise HTTPException(
                status_code=503,
                detail="Home relay was revoked but fail-closed state was not saved",
            ) from exc
        if isinstance(exc, IntegrityError):
            raise HTTPException(
                status_code=409,
                detail="Home relay setup conflict",
            ) from exc
        raise HTTPException(
            status_code=500,
            detail="Home relay setup database commit failed",
        ) from exc

    base_url = app_settings.home_relay_public_base_url.rstrip("/")
    script_url = f"{base_url}/api/proxy-routes/home-relay/installer.zip"
    pairing_url = f"{base_url}/api/proxy-routes/home-relay/enroll"
    command = _build_home_relay_setup_command(
        script_url=script_url,
        pairing_url=pairing_url,
        pairing_token=raw_token,
        installer_sha256=installer_sha256,
        autostart=req.autostart,
    )
    response.headers["Cache-Control"] = "no-store"
    return HomeRelaySetupOut(
        setup_token=raw_token,
        expires_at=expires_at,
        powershell_command=command,
        script_download_url=script_url,
        installer_sha256=installer_sha256,
    )


@router.patch("/{route_id}", response_model=ProxyRouteOut)
async def update_proxy_route(
    route_id: int,
    req: ProxyRouteUpdate,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> ProxyRouteOut:
    changes = req.model_dump(exclude_unset=True)
    is_default = changes.pop("is_default", None)
    clear_credentials = changes.pop("clear_credentials", False)
    route_snapshot = await _route_or_404(session, route_id)
    settings_snapshot = await _settings(session)
    transport_fields = {"host", "port", "proxy_type", "username", "password"}
    transport_patch_requested = bool(transport_fields & set(changes)) or clear_credentials
    route_specific_change_requested = (
        transport_patch_requested or "enabled" in changes
    )
    default_change_requested = (
        is_default is True
        and settings_snapshot.default_proxy_route_id != route_id
    ) or (
        is_default is False
        and settings_snapshot.default_proxy_route_id == route_id
    )
    affected_accounts: list[Account] = []
    if route_specific_change_requested or default_change_requested:
        affected_accounts = await _lock_accounts_for_route_change(
            session,
            route_id=route_id if route_specific_change_requested else None,
            include_inherited=(
                default_change_requested
                or (
                    route_specific_change_requested
                    and settings_snapshot.default_proxy_route_id == route_id
                )
            ),
        )

    route = await _route_for_update_or_404(session, route_snapshot.id)
    settings = await _settings_for_update(session)
    if route.mode == "home_relay" and {
        "host", "port", "proxy_type", "username", "password"
    } & set(changes):
        raise HTTPException(
            status_code=422,
            detail="Home relay transport is managed automatically",
        )

    if transport_patch_requested and (
        settings.default_proxy_route_id == route.id
        or await _route_has_account_reference(session, route.id)
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Unassign this route from the default and every account "
                "before changing its connection settings"
            ),
        )
    transport_changed = any(
        (
            route.username_encrypted if field == "username"
            else route.password_encrypted if field == "password"
            else getattr(route, field)
        )
        != changes[field]
        for field in transport_fields & set(changes)
    ) or (
        clear_credentials
        and bool(route.username_encrypted or route.password_encrypted)
    )
    enabled_changed = (
        "enabled" in changes and changes["enabled"] != route.enabled
    )
    resulting_proxy_type = changes.get("proxy_type", route.proxy_type)
    supplies_credentials = bool({"username", "password"} & set(changes))
    if resulting_proxy_type == "socks5" and (
        supplies_credentials
        or (
            bool(route.username_encrypted or route.password_encrypted)
            and not clear_credentials
        )
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "Playwright SOCKS5 routes cannot use username/password; "
                "clear credentials first"
            ),
        )
    if changes.get("enabled") is False and settings.default_proxy_route_id == route.id:
        raise HTTPException(
            status_code=409,
            detail="Choose Direct or another default route before disabling it",
        )
    if is_default is True and (
        not route.enabled
        or route.status != "online"
        or not proxy_route_check_is_fresh(route.last_checked_at)
        or transport_changed
        or changes.get("enabled") is False
    ):
        raise HTTPException(
            status_code=409,
            detail="Only an unchanged, tested online route can be default",
        )
    for field, value in changes.items():
        if field == "username":
            route.username_encrypted = value
        elif field == "password":
            route.password_encrypted = value
        else:
            setattr(route, field, value)
    if clear_credentials:
        route.username_encrypted = None
        route.password_encrypted = None
    if transport_changed or enabled_changed:
        route.status = "unchecked"
        route.egress_ip = None
        route.latency_ms = None
        route.last_checked_at = None
        route.last_error = None
    if transport_changed or enabled_changed:
        route.config_revision += 1
    default_changed = False
    if is_default is True:
        default_changed = settings.default_proxy_route_id != route.id
        settings.default_proxy_route_id = route.id
    elif is_default is False and settings.default_proxy_route_id == route.id:
        default_changed = True
        settings.default_proxy_route_id = None
    route.updated_at = datetime.now(timezone.utc)
    queued = 0
    if transport_changed or enabled_changed or default_changed:
        queued = await _queue_accounts_after_route_change(
            session,
            affected_accounts,
            route_id=(
                route.id
                if settings.default_proxy_route_id == route.id
                or any(account.proxy_route_id == route.id for account in affected_accounts)
                else None
            ),
        )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Proxy route name already exists")
    notify_capacity_changed(request)
    if queued:
        notify_validation_queued(request)
    await session.refresh(route)
    return _route_out(route, settings.default_proxy_route_id)


@router.delete("/{route_id}", status_code=204)
async def delete_proxy_route(
    route_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    route = await _route_for_update_or_404(session, route_id)
    settings = await _settings_for_update(session)
    await _lock_home_relay_setups(session, route.id)
    if settings.default_proxy_route_id == route.id:
        raise HTTPException(
            status_code=409,
            detail="Choose Direct or another default route before deleting it",
        )
    account_reference = (
        await session.execute(
            select(Account.id).where(Account.proxy_route_id == route.id).limit(1)
        )
    ).scalar_one_or_none()
    if account_reference is not None:
        raise HTTPException(
            status_code=409,
            detail="Reassign accounts before deleting this proxy route",
        )
    if route.mode == "home_relay":
        # Publish a durable fail-closed state *before* revoking the external
        # SSH key. If a later revoke/delete/DB operation fails, no account can
        # keep selecting a route whose tunnel has already disappeared.
        route.enabled = False
        route.status = "offline"
        route.egress_ip = None
        route.latency_ms = None
        route.last_error = "relay_deleting"
        route.config_revision += 1
        route.updated_at = datetime.now(timezone.utc)
        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            raise HTTPException(
                status_code=503,
                detail="Home relay could not be disabled before deletion",
            ) from exc
        try:
            await revoke_authorized_key(route.id)
        except OSError as exc:
            route = await _route_for_update_or_404(session, route_id)
            route.last_error = "relay_session_ack_timeout"
            route.updated_at = datetime.now(timezone.utc)
            await session.commit()
            raise HTTPException(
                status_code=503,
                detail="Home relay key could not be revoked",
            ) from exc
    await session.execute(
        delete(HomeRelaySetup).where(HomeRelaySetup.route_id == route.id)
    )
    await session.delete(route)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        persisted = await session.get(ProxyRoute, route_id)
        if persisted is not None:
            persisted.status = "offline"
            persisted.last_error = "proxy_route_reference_changed"
            persisted.config_revision += 1
            persisted.updated_at = datetime.now(timezone.utc)
            await session.commit()
        raise HTTPException(
            status_code=409,
            detail="Proxy route became referenced while it was being deleted",
        ) from exc
    except Exception as exc:
        await session.rollback()
        # A home relay was already committed disabled before its key was
        # revoked, so a generic DB failure cannot resurrect a selectable but
        # dead route. Custom routes have no external side effect to compensate.
        raise HTTPException(
            status_code=503,
            detail="Proxy route deletion could not be completed",
        ) from exc
    notify_capacity_changed(request)
    return Response(status_code=204)


@router.post("/{route_id}/test", response_model=ProxyRouteOut)
async def test_proxy_route(
    route_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> ProxyRouteOut:
    route = await _route_or_404(session, route_id)
    tested_revision = route.config_revision
    result = await probe_proxy_route(route)
    published = await publish_proxy_probe_result(
        session,
        route_id=route_id,
        tested_revision=tested_revision,
        result=result,
    )
    if not published:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Proxy route changed while it was being tested; test it again",
        )
    await session.commit()
    notify_capacity_changed(request)
    await session.refresh(route)
    settings = await session.get(SellerSettings, 1)
    return _route_out(
        route,
        settings.default_proxy_route_id if settings is not None else None,
    )


@public_router.get("/home-relay/installer.zip", include_in_schema=False)
async def download_home_relay_installer(request: Request) -> StreamingResponse:
    await _enforce_public_rate_limit(
        _INSTALLER_REQUESTS,
        _INSTALLER_GLOBAL_REQUESTS,
        request=request,
        per_client_limit=_INSTALLER_REQUEST_LIMIT,
        global_limit=_INSTALLER_GLOBAL_REQUEST_LIMIT,
    )
    try:
        archive_bytes = await _home_relay_installer_archive()
    except OSError:
        raise HTTPException(status_code=503, detail="Installer is unavailable")
    return StreamingResponse(
        BytesIO(archive_bytes),
        media_type="application/zip",
        headers={
            "Cache-Control": "no-cache",
            "Content-Disposition": (
                'attachment; filename="funpay-home-relay-installer.zip"'
            ),
        },
    )


@public_router.post("/home-relay/enroll", response_model=HomeRelayEnrollOut)
async def enroll_home_relay(
    req: HomeRelayEnrollRequest,
    response: Response,
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> HomeRelayEnrollOut:
    await _enforce_public_rate_limit(
        _ENROLL_REQUESTS,
        _ENROLL_GLOBAL_REQUESTS,
        request=request,
        per_client_limit=_ENROLL_REQUEST_LIMIT,
        global_limit=_ENROLL_GLOBAL_REQUEST_LIMIT,
    )
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Invalid or expired setup token")
    raw_token = authorization[len(prefix):].strip()
    if not raw_token or len(raw_token) > 256:
        raise HTTPException(status_code=401, detail="Invalid or expired setup token")
    setup_token_hash = token_hash(raw_token)
    # Read identity without a row lock only to discover route_id.  Then acquire
    # every durable lock in the global order route -> setup and re-read all
    # security-sensitive setup state under that lock.
    setup_identity = (
        await session.execute(
            select(HomeRelaySetup.id, HomeRelaySetup.route_id).where(
                HomeRelaySetup.token_hash == setup_token_hash
            )
        )
    ).one_or_none()
    if setup_identity is None:
        raise HTTPException(status_code=401, detail="Invalid or expired setup token")
    route = (
        await session.execute(
            select(ProxyRoute)
            .where(ProxyRoute.id == setup_identity.route_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    setup = (
        await session.execute(
            select(HomeRelaySetup)
            .where(
                HomeRelaySetup.id == setup_identity.id,
                HomeRelaySetup.token_hash == setup_token_hash,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    expires_at = setup.expires_at if setup is not None else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if (
        setup is None
        or setup.consumed_at is not None
        or expires_at is None
        or expires_at <= now
    ):
        raise HTTPException(status_code=401, detail="Invalid or expired setup token")
    if (
        route is None
        or route.mode != "home_relay"
        or route.id != setup.route_id
    ):
        raise HTTPException(status_code=401, detail="Invalid or expired setup token")
    try:
        public_key, fingerprint = validate_public_key(req.public_key)
        host_key_type, host_key_data = read_ssh_host_public_key()
        await install_authorized_key(
            route_id=route.id,
            public_key=public_key,
            fingerprint=fingerprint,
        )
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid SSH public key")
    except OSError as exc:
        route.enabled = False
        route.status = "offline"
        route.last_error = "relay_session_ack_timeout"
        route.config_revision += 1
        route.updated_at = now
        await session.commit()
        raise HTTPException(
            status_code=503, detail="Home relay did not confirm session rotation"
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Home relay is unavailable") from exc

    setup.consumed_at = now
    setup.machine_name = req.machine_name
    setup.public_key_fingerprint = fingerprint
    route.status = "unchecked"
    route.enabled = True
    route.last_error = None
    route.config_revision += 1
    route.updated_at = now
    route_id = route.id
    route_name = route.name
    try:
        await session.commit()
    except Exception as commit_exc:
        await session.rollback()
        compensation_error: Exception | None = None
        try:
            # Never return from a failed DB enrollment while its SSH key can
            # still open or retain a tunnel.
            await revoke_authorized_key(route_id)
        except Exception as exc:
            compensation_error = exc

        state_error: Exception | None = None
        try:
            persisted = (
                await session.execute(
                    select(ProxyRoute)
                    .where(ProxyRoute.id == route_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if persisted is not None:
                persisted.enabled = False
                persisted.status = "offline"
                persisted.last_error = "enrollment_commit_failed"
                persisted.config_revision += 1
                persisted.updated_at = datetime.now(timezone.utc)
            await session.commit()
        except Exception as exc:  # pragma: no cover - database outage path
            await session.rollback()
            state_error = exc

        if compensation_error is not None or state_error is not None:
            raise HTTPException(
                status_code=503,
                detail="Enrollment failed and rollback could not be confirmed",
            ) from (compensation_error or state_error)
        raise HTTPException(
            status_code=500,
            detail="Enrollment database commit failed; SSH key was revoked",
        ) from commit_exc
    response.headers["Cache-Control"] = "no-store"
    app_settings = get_settings()
    return HomeRelayEnrollOut(
        relay_id=f"relay-{route_id}-{fingerprint[:12]}",
        display_name=route_name,
        ssh_host=app_settings.home_relay_public_host,
        ssh_port=app_settings.home_relay_ssh_port,
        ssh_user=app_settings.home_relay_ssh_user,
        remote_socks_bind="0.0.0.0",
        remote_socks_port=app_settings.home_relay_proxy_port,
        host_key=HomeRelayHostKey(type=host_key_type, data=host_key_data),
    )
