from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import time

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.config import get_settings
from app.integrations.playwright.browser import browser_context
from app.integrations.playwright.proxy import (
    BrowserProxy,
    ProxyUnavailableError,
    is_proxy_failure,
)
from app.models.account import Account
from app.models.proxy_route import ProxyRoute
from app.models.settings import SellerSettings


_PUBLIC_KEY_RE = re.compile(
    r"^ssh-ed25519\s+([A-Za-z0-9+/]+={0,2})(?:\s+[^\r\n]{0,128})?$"
)
_AUTHORIZED_KEYS_LOCK = asyncio.Lock()
_IP_CHECK_URL = "https://api.ipify.org?format=json"
_ED25519_KEY_TYPE = b"ssh-ed25519"


@dataclass(frozen=True, slots=True)
class ProxyProbeResult:
    status: str
    checked_at: datetime
    egress_ip: str | None = None
    latency_ms: int | None = None
    error: str | None = None


async def resolve_browser_proxy(
    session: AsyncSession,
    account: Account,
) -> BrowserProxy | None:
    """Resolve account override then global default, with no direct fallback."""

    route_id = account.proxy_route_id
    if route_id is None:
        settings = await session.get(
            SellerSettings,
            1,
            populate_existing=True,
        )
        route_id = settings.default_proxy_route_id if settings is not None else None
    if route_id is None:
        return None

    route = await session.get(
        ProxyRoute,
        route_id,
        populate_existing=True,
    )
    if (
        route is None
        or not route.enabled
        or route.status != "online"
        or not proxy_route_check_is_fresh(route.last_checked_at)
    ):
        raise ProxyUnavailableError(
            "Настроенный маршрут входа не проверен или недоступен."
        )
    return browser_proxy_from_route(route)


def proxy_route_check_is_fresh(
    checked_at: datetime | None,
    *,
    now: datetime | None = None,
) -> bool:
    if checked_at is None:
        return False
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    else:
        checked_at = checked_at.astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    return checked_at >= now - timedelta(
        seconds=get_settings().proxy_route_max_age_seconds
    )


def effective_proxy_route_is_healthy(
    *,
    now: datetime | None = None,
) -> ColumnElement[bool]:
    """SQL predicate for accounts whose effective login route is usable.

    An account-specific route overrides the singleton system default. Direct
    mode remains eligible; a configured route must be enabled, online and
    recently probed. This predicate is shared by capacity and final-delivery
    checks so a sleeping home PC cannot leave inventory sellable.
    """

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(
        seconds=get_settings().proxy_route_max_age_seconds
    )
    default_route_id = (
        select(SellerSettings.default_proxy_route_id)
        .where(SellerSettings.id == 1)
        .scalar_subquery()
    )
    effective_route_id = func.coalesce(
        Account.proxy_route_id,
        default_route_id,
    )
    healthy_route = (
        select(ProxyRoute.id)
        .where(
            ProxyRoute.id == effective_route_id,
            ProxyRoute.enabled.is_(True),
            ProxyRoute.status == "online",
            ProxyRoute.last_checked_at.is_not(None),
            ProxyRoute.last_checked_at >= cutoff,
        )
        .exists()
    )
    return or_(effective_route_id.is_(None), healthy_route)


async def publish_proxy_probe_result(
    session: AsyncSession,
    *,
    route_id: int,
    tested_revision: int,
    result: ProxyProbeResult,
) -> bool:
    """Publish a probe only if the tested transport revision still exists."""

    updated = await session.execute(
        update(ProxyRoute)
        .where(
            ProxyRoute.id == route_id,
            ProxyRoute.config_revision == tested_revision,
            ProxyRoute.enabled.is_(True),
            or_(
                ProxyRoute.last_checked_at.is_(None),
                ProxyRoute.last_checked_at <= result.checked_at,
            ),
        )
        .values(
            status=result.status,
            egress_ip=result.egress_ip,
            latency_ms=result.latency_ms,
            last_checked_at=result.checked_at,
            last_error=result.error,
            updated_at=result.checked_at,
        )
        .execution_options(synchronize_session=False)
    )
    return updated.rowcount == 1


async def mark_proxy_route_offline(
    session: AsyncSession,
    proxy: BrowserProxy | None,
    *,
    error: str = "runtime_proxy_unavailable",
) -> bool:
    """Fail closed after a real routed operation proves transport failure."""

    if proxy is None or proxy.config_revision is None:
        return False
    checked_at = datetime.now(timezone.utc)
    updated = await session.execute(
        update(ProxyRoute)
        .where(
            ProxyRoute.id == proxy.route_id,
            ProxyRoute.config_revision == proxy.config_revision,
            ProxyRoute.enabled.is_(True),
        )
        .values(
            status="offline",
            egress_ip=None,
            latency_ms=None,
            last_checked_at=checked_at,
            last_error=error,
            updated_at=checked_at,
        )
        .execution_options(synchronize_session=False)
    )
    return updated.rowcount == 1


def browser_proxy_from_route(route: ProxyRoute) -> BrowserProxy:
    # Probes must be able to exercise an unchecked route. Runtime resolution
    # enforces ``status == online`` separately in ``resolve_browser_proxy``.
    if not route.enabled:
        raise ProxyUnavailableError("Маршрут входа отключён.")
    return BrowserProxy(
        route_id=route.id,
        proxy_type=route.proxy_type,
        host=route.host,
        port=route.port,
        username=route.username_encrypted or None,
        password=route.password_encrypted or None,
        config_revision=route.config_revision,
    )


async def probe_proxy_route(route: ProxyRoute) -> ProxyProbeResult:
    """Exercise a route through Chromium and report only safe diagnostics."""

    checked_at = datetime.now(timezone.utc)
    if not route.enabled:
        return ProxyProbeResult(
            status="offline",
            checked_at=checked_at,
            error="proxy_disabled",
        )
    proxy = browser_proxy_from_route(route)
    started = time.monotonic()
    try:
        async with browser_context(proxy=proxy) as context:
            page = await context.new_page()
            try:
                response = await page.goto(
                    _IP_CHECK_URL,
                    wait_until="domcontentloaded",
                    timeout=20_000,
                )
                if response is None or not response.ok:
                    raise ProxyUnavailableError()
                payload = json.loads(await page.text_content("body") or "{}")
                egress_ip = str(payload.get("ip") or "")
                ipaddress.ip_address(egress_ip)
            finally:
                await page.close()
    except Exception as exc:
        error = (
            "proxy_unavailable"
            if is_proxy_failure(exc) or isinstance(exc, ProxyUnavailableError)
            else "proxy_test_failed"
        )
        return ProxyProbeResult(
            status="offline",
            checked_at=checked_at,
            error=error,
        )
    return ProxyProbeResult(
        status="online",
        checked_at=checked_at,
        egress_ip=egress_ip,
        latency_ms=max(0, round((time.monotonic() - started) * 1000)),
    )


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_setup_token() -> str:
    return secrets.token_urlsafe(32)


def validate_public_key(public_key: str) -> tuple[str, str]:
    """Return canonical key and its SHA-256 fingerprint."""

    public_key = public_key.strip()
    match = _PUBLIC_KEY_RE.fullmatch(public_key)
    if match is None:
        raise ValueError("unsupported_public_key")
    raw = _decode_ed25519_public_blob(match.group(1))
    canonical = f"ssh-ed25519 {match.group(1)}"
    fingerprint = hashlib.sha256(raw).hexdigest()
    return canonical, fingerprint


def _decode_ed25519_public_blob(encoded: str) -> bytes:
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError("invalid_public_key") from exc
    expected = (
        len(_ED25519_KEY_TYPE).to_bytes(4, "big")
        + _ED25519_KEY_TYPE
        + (32).to_bytes(4, "big")
    )
    if len(raw) != len(expected) + 32 or not raw.startswith(expected):
        raise ValueError("invalid_public_key")
    return raw


async def install_authorized_key(
    *,
    route_id: int,
    public_key: str,
    fingerprint: str,
) -> None:
    """Atomically replace the enrolled key for one route in the shared file."""

    settings = get_settings()
    path = Path(settings.home_relay_authorized_keys_path)
    marker = f"funpay-relay-{route_id}-"
    # ``restrict`` disables shell/agent/X11/PTY. Re-enable only remote port
    # forwarding and limit it to the internal SOCKS listener.
    line = (
        "restrict,port-forwarding,permitlisten=\"0.0.0.0:"
        f"{settings.home_relay_proxy_port}\" {public_key} "
        f"{marker}{fingerprint[:16]}"
    )

    async with _AUTHORIZED_KEYS_LOCK:
        # Clear every key ever managed by this application, kill all sessions
        # authenticated with an old key, and only then make the new key valid.
        # The Windows client does not connect until enrollment returns.
        await asyncio.to_thread(_clear_managed_authorized_keys, path)
        await _rotate_relay_sessions(path)
        await asyncio.to_thread(_append_managed_authorized_key, path, line)


async def revoke_authorized_key(route_id: int) -> None:
    """Remove a relay key before deleting its database route."""

    del route_id  # singleton relay: revoke every stale app-managed key
    path = Path(get_settings().home_relay_authorized_keys_path)
    async with _AUTHORIZED_KEYS_LOCK:
        await asyncio.to_thread(_clear_managed_authorized_keys, path)
        await _rotate_relay_sessions(path)


async def _rotate_relay_sessions(authorized_keys_path: Path) -> None:
    """Ask the sidecar to kill every live SSH child and acknowledge it."""

    generation_path = authorized_keys_path.parent / "session_generation"
    ack_path = authorized_keys_path.parent / "session_generation.ack"
    generation = secrets.token_urlsafe(24)
    await asyncio.to_thread(
        _write_atomic_text,
        generation_path,
        generation + "\n",
    )
    await _wait_for_session_generation_ack(
        ack_path,
        generation,
        timeout=get_settings().home_relay_session_ack_timeout_seconds,
    )


async def _wait_for_session_generation_ack(
    ack_path: Path,
    generation: str,
    *,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            acknowledged = await asyncio.to_thread(
                ack_path.read_text, encoding="utf-8"
            )
        except (FileNotFoundError, OSError):
            acknowledged = ""
        if secrets.compare_digest(acknowledged.strip(), generation):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OSError("home_relay_session_ack_timeout")
        await asyncio.sleep(min(0.05, remaining))


def _write_atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _is_managed_authorized_key(line: str) -> bool:
    fields = line.strip().split()
    try:
        key_type_index = fields.index("ssh-ed25519")
    except ValueError:
        return False
    return (
        len(fields) == key_type_index + 3
        and fields[key_type_index + 2].startswith("funpay-relay-")
    )


def _clear_managed_authorized_keys(path: Path) -> None:
    if not path.exists():
        return
    existing = path.read_text(encoding="utf-8").splitlines()
    retained = [item for item in existing if not _is_managed_authorized_key(item)]
    if retained == existing:
        return
    _write_authorized_keys(path, retained)


def _append_managed_authorized_key(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    retained = [item for item in existing if not _is_managed_authorized_key(item)]
    retained.append(line)
    _write_authorized_keys(path, retained)


def _write_authorized_keys(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    temporary.write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def read_ssh_host_public_key() -> tuple[str, str]:
    path = Path(get_settings().home_relay_host_public_key_path)
    try:
        parts = path.read_text(encoding="utf-8").strip().split()
    except OSError as exc:
        raise ProxyUnavailableError(
            "Домашний relay ещё не подготовил SSH host key."
        ) from exc
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise ProxyUnavailableError("SSH host key домашнего relay повреждён.")
    # Validate before returning data to an installer that will pin this key.
    try:
        _decode_ed25519_public_blob(parts[1])
    except ValueError as exc:
        raise ProxyUnavailableError("SSH host key домашнего relay повреждён.") from exc
    return parts[0], parts[1]
