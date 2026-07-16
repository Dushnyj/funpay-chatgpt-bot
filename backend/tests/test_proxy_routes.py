from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import COOKIE_NAME, create_access_token
from app.integrations.playwright.proxy import (
    BrowserProxy,
    ProxyUnavailableError,
    is_proxy_failure,
)
from app.main import app
from app.models.account import Account, AccountCheckJob
from app.models.proxy_route import HomeRelaySetup, ProxyRoute
from app.models.settings import SellerSettings
from app.services.account_validation import AccountValidationError, validate_account
from app.services.proxy_routes import (
    ProxyProbeResult,
    browser_proxy_from_route,
    mark_proxy_route_offline,
    publish_proxy_probe_result,
    browser_proxy_from_route,
    resolve_browser_proxy,
)
import app.api.routers.proxy_routes as proxy_routes_router
import app.services.proxy_routes as proxy_service


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    token = create_access_token()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set(COOKIE_NAME, token)
        yield client


@pytest.fixture
def reset_public_rate_limits():
    state_names = (
        "_INSTALLER_REQUESTS",
        "_INSTALLER_GLOBAL_REQUESTS",
        "_ENROLL_REQUESTS",
        "_ENROLL_GLOBAL_REQUESTS",
    )
    for name in state_names:
        state = getattr(proxy_routes_router, name, None)
        if state is not None:
            state.clear()
    yield
    for name in state_names:
        state = getattr(proxy_routes_router, name, None)
        if state is not None:
            state.clear()


async def _request_public_rate_limited_endpoint(
    client: AsyncClient,
    *,
    method: str,
    path: str,
    forwarded_for: str,
):
    headers = {"X-Forwarded-For": forwarded_for}
    if method == "GET":
        return await client.get(path, headers=headers)
    return await client.post(
        path,
        headers=headers,
        json={
            "schema_version": 1,
            "machine_name": "WORKSTATION",
            "display_name": "Rate limit test",
            "public_key": "ssh-ed25519 " + ("A" * 32),
            "client_version": "1.0.0",
        },
    )


_PUBLIC_RATE_LIMIT_CASES = (
    pytest.param(
        "GET",
        "/api/proxy-routes/home-relay/installer.zip",
        "_INSTALLER_REQUEST_LIMIT",
        "_INSTALLER_GLOBAL_REQUEST_LIMIT",
        "_INSTALLER_REQUESTS",
        "_INSTALLER_GLOBAL_REQUESTS",
        200,
        id="installer",
    ),
    pytest.param(
        "POST",
        "/api/proxy-routes/home-relay/enroll",
        "_ENROLL_REQUEST_LIMIT",
        "_ENROLL_GLOBAL_REQUEST_LIMIT",
        "_ENROLL_REQUESTS",
        "_ENROLL_GLOBAL_REQUESTS",
        401,
        id="enroll",
    ),
)


async def _create_online_route(
    session: AsyncSession,
    *,
    name: str = "Residential",
) -> ProxyRoute:
    route = ProxyRoute(
        name=name,
        mode="custom_proxy",
        proxy_type="http",
        host="proxy.example.test",
        port=3128,
        status="online",
        enabled=True,
        last_checked_at=datetime.now(timezone.utc),
    )
    session.add(route)
    await session.commit()
    return route


async def test_default_route_change_invalidates_inheriting_accounts(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    route_a = await _create_online_route(session, name="Default guard A")
    route_b = await _create_online_route(session, name="Default guard B")
    settings = SellerSettings(id=1, default_proxy_route_id=route_a.id)
    account = Account(
        login="default-guard@example.com",
        password_encrypted="password",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        proxy_route_id=None,
        status="active",
        chatgpt_last_check_at=datetime.now(timezone.utc),
    )
    session.add_all([settings, account])
    await session.commit()

    response = await auth_client.put(
        "/api/proxy-routes/default",
        json={"route_id": route_b.id},
    )

    assert response.status_code == 200
    await session.refresh(settings)
    await session.refresh(account)
    assert settings.default_proxy_route_id == route_b.id
    assert account.status == "pending_validation"
    assert account.chatgpt_last_check_at is None
    job = (
        await session.execute(
            select(AccountCheckJob).where(AccountCheckJob.account_id == account.id)
        )
    ).scalar_one()
    assert job.status == "pending"
    assert job.job_type == "full_validation"


async def test_default_and_config_changes_reject_active_validation(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    route_a = await _create_online_route(session, name="Busy guard A")
    route_b = await _create_online_route(session, name="Busy guard B")
    settings = SellerSettings(id=1, default_proxy_route_id=route_a.id)
    account = Account(
        login="busy-route-guard@example.com",
        password_encrypted="password",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        proxy_route_id=None,
        status="active",
    )
    session.add_all([settings, account])
    await session.flush()
    job = AccountCheckJob(
        account_id=account.id,
        priority="manual",
        job_type="full_validation",
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    session.add(job)
    await session.commit()

    default_response = await auth_client.put(
        "/api/proxy-routes/default",
        json={"route_id": route_b.id},
    )
    config_response = await auth_client.patch(
        f"/api/proxy-routes/{route_a.id}",
        json={"enabled": False},
    )

    assert default_response.status_code == 409
    assert config_response.status_code == 409
    await session.refresh(settings)
    await session.refresh(route_a)
    await session.refresh(account)
    assert settings.default_proxy_route_id == route_a.id
    assert route_a.enabled is True
    assert account.status == "active"


def _configure_home_relay(monkeypatch, tmp_path: Path) -> tuple[str, Path]:
    from app.config import get_settings

    key_type = b"ssh-ed25519"
    key_blob = (
        len(key_type).to_bytes(4, "big")
        + key_type
        + (32).to_bytes(4, "big")
        + (b"k" * 32)
    )
    key_material = base64.b64encode(key_blob).decode()
    host_key_path = tmp_path / "ssh_host_ed25519_key.pub"
    authorized_keys_path = tmp_path / "authorized_keys"
    host_key_path.write_text(f"ssh-ed25519 {key_material}\n", encoding="utf-8")
    authorized_keys_path.write_text("", encoding="utf-8")
    monkeypatch.setenv(
        "HOME_RELAY_AUTHORIZED_KEYS_PATH", str(authorized_keys_path)
    )
    monkeypatch.setenv("HOME_RELAY_HOST_PUBLIC_KEY_PATH", str(host_key_path))
    monkeypatch.setenv(
        "HOME_RELAY_PUBLIC_BASE_URL", "https://admin.example.test"
    )
    monkeypatch.setenv("HOME_RELAY_PUBLIC_HOST", "relay.example.test")
    get_settings.cache_clear()
    return key_material, authorized_keys_path


async def _ack_session_rotation(
    ack_path: Path,
    generation: str,
    *,
    timeout: float,
) -> None:
    del timeout
    ack_path.parent.mkdir(parents=True, exist_ok=True)
    ack_path.write_text(generation + "\n", encoding="utf-8")


def _decoded_protected_setup_body(command: str) -> str:
    marker = "-EncodedCommand '"
    encoded = command.split(marker, 1)[1].split("'", 1)[0]
    return base64.b64decode(encoded).decode("utf-16-le")


def _assert_windows_powershell_51_parses(source: str) -> None:
    executable = (
        Path(os.environ["WINDIR"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    environment = os.environ.copy()
    environment["FUNPAY_TEST_SCRIPT"] = base64.b64encode(
        source.encode("utf-16-le")
    ).decode("ascii")
    parser = (
        "$source = [Text.Encoding]::Unicode.GetString("
        "[Convert]::FromBase64String($env:FUNPAY_TEST_SCRIPT)); "
        "$tokens = $null; $errors = $null; "
        "[Management.Automation.Language.Parser]::ParseInput("
        "$source, [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count -ne 0) { $errors | ForEach-Object { "
        "[Console]::Error.WriteLine($_.Message) }; exit 1 }"
    )
    completed = subprocess.run(
        [
            str(executable),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            parser,
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_https_connect_category_uses_playwright_http_transport():
    proxy = BrowserProxy(
        route_id=1,
        proxy_type="https",
        host="proxy.example.test",
        port=443,
    )
    assert proxy.server == "http://proxy.example.test:443"
    assert is_proxy_failure(RuntimeError("net::ERR_INVALID_AUTH_CREDENTIALS"))


def test_enrollment_rejects_non_openssh_ed25519_blob():
    malformed = base64.b64encode(b"k" * 64).decode()

    with pytest.raises(ValueError, match="invalid_public_key"):
        proxy_service.validate_public_key(f"ssh-ed25519 {malformed}")


@pytest.mark.parametrize(
    (
        "method",
        "path",
        "per_client_limit_name",
        "global_limit_name",
        "requests_name",
        "global_requests_name",
        "allowed_status",
    ),
    _PUBLIC_RATE_LIMIT_CASES,
)
async def test_public_rate_limit_isolates_clients_and_ignores_raw_proxy_header(
    monkeypatch,
    reset_public_rate_limits,
    method: str,
    path: str,
    per_client_limit_name: str,
    global_limit_name: str,
    requests_name: str,
    global_requests_name: str,
    allowed_status: int,
):
    del reset_public_rate_limits, global_requests_name
    configured_per_client = getattr(
        proxy_routes_router,
        per_client_limit_name,
    )
    configured_global = getattr(
        proxy_routes_router,
        global_limit_name,
        configured_per_client,
    )
    assert configured_global >= configured_per_client * 100
    monkeypatch.setattr(proxy_routes_router, per_client_limit_name, 2)
    monkeypatch.setattr(
        proxy_routes_router,
        global_limit_name,
        8,
        raising=False,
    )
    monkeypatch.setattr(
        proxy_routes_router,
        "_home_relay_installer_archive",
        AsyncMock(return_value=b"test archive"),
    )

    async with (
        AsyncClient(
            transport=ASGITransport(
                app=app,
                client=("198.51.100.10", 41000),
            ),
            base_url="http://test",
        ) as limited_client,
        AsyncClient(
            transport=ASGITransport(
                app=app,
                client=("198.51.100.11", 42000),
            ),
            base_url="http://test",
        ) as other_client,
    ):
        first = await _request_public_rate_limited_endpoint(
            limited_client,
            method=method,
            path=path,
            forwarded_for="203.0.113.1",
        )
        second = await _request_public_rate_limited_endpoint(
            limited_client,
            method=method,
            path=path,
            forwarded_for="203.0.113.2",
        )
        blocked = await _request_public_rate_limited_endpoint(
            limited_client,
            method=method,
            path=path,
            forwarded_for="203.0.113.3",
        )
        isolated = await _request_public_rate_limited_endpoint(
            other_client,
            method=method,
            path=path,
            forwarded_for="203.0.113.3",
        )

    assert [first.status_code, second.status_code] == [
        allowed_status,
        allowed_status,
    ]
    assert blocked.status_code == 429
    assert isolated.status_code == allowed_status
    attempts_by_client = getattr(proxy_routes_router, requests_name)
    assert len(attempts_by_client) == 2
    assert all(
        isinstance(client_key, bytes) and len(client_key) == 16
        for client_key in attempts_by_client
    )
    assert "198.51.100.10" not in repr(attempts_by_client)
    assert "198.51.100.11" not in repr(attempts_by_client)


@pytest.mark.parametrize(
    (
        "method",
        "path",
        "per_client_limit_name",
        "global_limit_name",
        "requests_name",
        "global_requests_name",
        "allowed_status",
    ),
    _PUBLIC_RATE_LIMIT_CASES,
)
async def test_public_rate_limit_enforces_bounded_global_ceiling(
    monkeypatch,
    reset_public_rate_limits,
    method: str,
    path: str,
    per_client_limit_name: str,
    global_limit_name: str,
    requests_name: str,
    global_requests_name: str,
    allowed_status: int,
):
    del reset_public_rate_limits
    monkeypatch.setattr(proxy_routes_router, per_client_limit_name, 5)
    monkeypatch.setattr(
        proxy_routes_router,
        global_limit_name,
        3,
        raising=False,
    )
    monkeypatch.setattr(
        proxy_routes_router,
        "_home_relay_installer_archive",
        AsyncMock(return_value=b"test archive"),
    )

    responses = []
    for index in range(4):
        client_host = f"198.51.100.{20 + index}"
        async with AsyncClient(
            transport=ASGITransport(
                app=app,
                client=(client_host, 43000 + index),
            ),
            base_url="http://test",
        ) as client:
            responses.append(
                await _request_public_rate_limited_endpoint(
                    client,
                    method=method,
                    path=path,
                    forwarded_for=f"203.0.113.{20 + index}",
                )
            )

    assert [response.status_code for response in responses[:3]] == [
        allowed_status,
        allowed_status,
        allowed_status,
    ]
    assert responses[3].status_code == 429
    assert len(getattr(proxy_routes_router, requests_name)) == 3
    assert len(getattr(proxy_routes_router, global_requests_name)) == 3


async def test_proxy_crud_encrypts_and_never_returns_password(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    session.add(SellerSettings(id=1))
    await session.commit()
    password = "very-secret-proxy-password"
    response = await auth_client.post(
        "/api/proxy-routes",
        json={
            "name": "Office HTTP",
            "mode": "custom_proxy",
            "proxy_type": "http",
            "host": "proxy.example.test",
            "port": 8080,
            "username": "proxy-user",
            "password": password,
        },
    )

    assert response.status_code == 201
    route_id = response.json()["id"]
    assert response.json()["has_password"] is True
    assert response.json()["username"] == "proxy-user"
    assert password not in response.text
    assert "password" not in response.json()

    listed = await auth_client.get("/api/proxy-routes")
    assert listed.status_code == 200
    assert password not in listed.text
    raw = (
        await session.execute(
            text(
                "SELECT username_encrypted, password_encrypted "
                "FROM proxy_routes WHERE id=:route_id"
            ),
            {"route_id": route_id},
        )
    ).one()
    assert raw.username_encrypted != "proxy-user"
    assert raw.password_encrypted != password

    cleared = await auth_client.patch(
        f"/api/proxy-routes/{route_id}",
        json={"clear_credentials": True},
    )
    assert cleared.status_code == 200
    assert cleared.json()["has_password"] is False
    assert cleared.json()["username"] is None


async def test_network_changes_increment_revision_and_invalidate_probe(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    session.add(SellerSettings(id=1))
    await session.commit()
    created = await auth_client.post(
        "/api/proxy-routes",
        json={
            "name": "Revision route",
            "mode": "custom_proxy",
            "proxy_type": "http",
            "host": "proxy-one.example.test",
            "port": 8080,
        },
    )
    assert created.status_code == 201
    assert created.json()["config_revision"] == 1
    route_id = created.json()["id"]

    renamed = await auth_client.patch(
        f"/api/proxy-routes/{route_id}",
        json={"name": "Renamed route"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["config_revision"] == 1

    changed = await auth_client.patch(
        f"/api/proxy-routes/{route_id}",
        json={"host": "proxy-two.example.test"},
    )
    assert changed.status_code == 200
    assert changed.json()["config_revision"] == 2
    assert changed.json()["status"] == "unchecked"
    assert changed.json()["egress_ip"] is None

    disabled = await auth_client.patch(
        f"/api/proxy-routes/{route_id}",
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["config_revision"] == 3
    assert disabled.json()["status"] == "unchecked"


async def test_transport_changes_are_blocked_while_route_is_referenced(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    route = await _create_online_route(session)
    settings = SellerSettings(id=1, default_proxy_route_id=route.id)
    session.add(settings)
    await session.commit()

    default_change = await auth_client.patch(
        f"/api/proxy-routes/{route.id}",
        json={"port": 8080},
    )
    assert default_change.status_code == 409

    settings.default_proxy_route_id = None
    session.add(
        Account(
            login="route-reference@example.com",
            password_encrypted="password",
            totp_secret_encrypted="JBSWY3DPEHPK3PXP",
            proxy_route_id=route.id,
        )
    )
    await session.commit()
    account_change = await auth_client.patch(
        f"/api/proxy-routes/{route.id}",
        json={"host": "replacement.example.test"},
    )
    assert account_change.status_code == 409


async def test_reference_writes_and_network_patch_lock_proxy_route(
    auth_client: AsyncClient,
    session: AsyncSession,
    test_engine,
):
    session.add(SellerSettings(id=1))
    route = await _create_online_route(session, name="Serialized route")
    locked_tables: list[str] = []

    def record_route_lock(
        _connection,
        clause,
        _multiparams,
        _params,
        _execution_options,
    ):
        if getattr(clause, "_for_update_arg", None) is None:
            return
        table_names = {
            table.name
            for table in clause.get_final_froms()
            if getattr(table, "name", None)
        }
        if "proxy_routes" in table_names:
            locked_tables.append("proxy_routes")
        elif "seller_settings" in table_names:
            locked_tables.append("seller_settings")

    event.listen(test_engine.sync_engine, "before_execute", record_route_lock)
    try:
        created = await auth_client.post(
            "/api/accounts",
            json={
                "login": "serialized-assignment@example.com",
                "password": "password",
                "totp_secret": "JBSWY3DPEHPK3PXP",
                "proxy_route_id": route.id,
            },
        )
        assert created.status_code == 201
        assert locked_tables == ["proxy_routes"]

        initial_job = (
            await session.execute(
                select(AccountCheckJob).where(
                    AccountCheckJob.account_id == created.json()["id"]
                )
            )
        ).scalar_one()
        initial_job.status = "done"
        initial_job.result = "test_setup"
        await session.commit()

        locked_tables.clear()
        made_default = await auth_client.put(
            "/api/proxy-routes/default",
            json={"route_id": route.id},
        )
        assert made_default.status_code == 200
        assert locked_tables[:2] == ["proxy_routes", "seller_settings"]

        await auth_client.put(
            "/api/proxy-routes/default",
            json={"route_id": None},
        )
        locked_tables.clear()
        blocked_patch = await auth_client.patch(
            f"/api/proxy-routes/{route.id}",
            json={"host": "new-serialized.example.test"},
        )
        assert blocked_patch.status_code == 409
        assert locked_tables[:2] == ["proxy_routes", "seller_settings"]
    finally:
        event.remove(
            test_engine.sync_engine,
            "before_execute",
            record_route_lock,
        )


async def test_repair_invalidates_stale_online_state_before_assignment(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
    tmp_path: Path,
):
    _configure_home_relay(monkeypatch, tmp_path)
    monkeypatch.setattr(
        proxy_service,
        "_wait_for_session_generation_ack",
        _ack_session_rotation,
    )
    session.add(SellerSettings(id=1))
    route = ProxyRoute(
        name="Previously online home",
        mode="home_relay",
        proxy_type="socks5",
        host="home-relay",
        port=1080,
        status="online",
        enabled=True,
    )
    session.add(route)
    await session.commit()

    repaired = await auth_client.post(
        "/api/proxy-routes/home-relay/setup",
        json={"name": "Previously online home", "autostart": False},
    )
    assert repaired.status_code == 201
    assignment = await auth_client.post(
        "/api/accounts",
        json={
            "login": "stale-online@example.com",
            "password": "password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "proxy_route_id": route.id,
        },
    )
    made_default = await auth_client.put(
        "/api/proxy-routes/default",
        json={"route_id": route.id},
    )

    assert assignment.status_code == 422
    assert made_default.status_code == 409


async def test_duplicate_name_repair_fails_before_revoking_existing_key(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
    tmp_path: Path,
):
    key_material, authorized_keys_path = _configure_home_relay(
        monkeypatch, tmp_path
    )
    session.add(SellerSettings(id=1))
    occupied = await _create_online_route(session, name="Occupied name")
    home = ProxyRoute(
        name="Existing home",
        mode="home_relay",
        proxy_type="socks5",
        host="home-relay",
        port=1080,
        status="online",
        enabled=True,
    )
    session.add(home)
    await session.commit()
    home_id = home.id
    existing_key = (
        "restrict,port-forwarding,permitlisten=\"0.0.0.0:1080\" "
        f"ssh-ed25519 {key_material} funpay-relay-{home_id}-existing"
    )
    authorized_keys_path.write_text(existing_key + "\n", encoding="utf-8")

    async def unexpected_rotation(*_args, **_kwargs):
        pytest.fail("duplicate name must be rejected before SSH revocation")

    monkeypatch.setattr(
        proxy_service,
        "_wait_for_session_generation_ack",
        unexpected_rotation,
    )
    response = await auth_client.post(
        "/api/proxy-routes/home-relay/setup",
        json={"name": occupied.name, "autostart": False},
    )

    assert response.status_code == 409
    assert authorized_keys_path.read_text(encoding="utf-8") == existing_key + "\n"
    assert not (tmp_path / "session_generation").exists()
    session.expire_all()
    persisted = await session.get(ProxyRoute, home_id)
    assert persisted is not None
    assert persisted.name == "Existing home"
    assert persisted.enabled is True
    assert persisted.status == "online"


async def test_repair_commit_failure_persists_fail_closed_route_state(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
    tmp_path: Path,
):
    key_material, authorized_keys_path = _configure_home_relay(
        monkeypatch, tmp_path
    )
    monkeypatch.setattr(
        proxy_service,
        "_wait_for_session_generation_ack",
        _ack_session_rotation,
    )
    session.add(SellerSettings(id=1))
    home = ProxyRoute(
        name="Commit failure home",
        mode="home_relay",
        proxy_type="socks5",
        host="home-relay",
        port=1080,
        status="online",
        enabled=True,
    )
    session.add(home)
    await session.commit()
    home_id = home.id
    authorized_keys_path.write_text(
        "restrict,port-forwarding,permitlisten=\"0.0.0.0:1080\" "
        f"ssh-ed25519 {key_material} funpay-relay-{home_id}-existing\n",
        encoding="utf-8",
    )
    original_commit = session.commit
    fail_next_commit = True

    async def fail_repair_commit_once():
        nonlocal fail_next_commit
        if fail_next_commit:
            fail_next_commit = False
            raise RuntimeError("simulated repair commit failure")
        await original_commit()

    monkeypatch.setattr(session, "commit", fail_repair_commit_once)
    response = await auth_client.post(
        "/api/proxy-routes/home-relay/setup",
        json={"name": "Renamed after revoke", "autostart": False},
    )

    assert response.status_code == 500
    assert not authorized_keys_path.read_text(encoding="utf-8")
    session.expire_all()
    persisted = await session.get(ProxyRoute, home_id)
    assert persisted is not None
    assert persisted.name == "Commit failure home"
    assert persisted.enabled is False
    assert persisted.status == "offline"
    assert persisted.last_error == "repair_commit_failed"


async def test_probe_result_is_rejected_if_route_changes_in_flight(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    session.add(SellerSettings(id=1))
    await session.commit()
    created = await auth_client.post(
        "/api/proxy-routes",
        json={
            "name": "Race route",
            "mode": "custom_proxy",
            "proxy_type": "http",
            "host": "before.example.test",
            "port": 8080,
        },
    )
    route_id = created.json()["id"]

    async def mutate_during_probe(_route):
        await session.execute(
            update(ProxyRoute)
            .where(ProxyRoute.id == route_id)
            .values(
                host="after.example.test",
                config_revision=ProxyRoute.config_revision + 1,
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return ProxyProbeResult(
            status="online",
            checked_at=datetime.now(timezone.utc),
            egress_ip="203.0.113.77",
            latency_ms=12,
        )

    monkeypatch.setattr(
        "app.api.routers.proxy_routes.probe_proxy_route",
        mutate_during_probe,
    )
    response = await auth_client.post(f"/api/proxy-routes/{route_id}/test")

    assert response.status_code == 409
    session.expire_all()
    persisted = await session.get(ProxyRoute, route_id)
    assert persisted is not None
    assert persisted.host == "after.example.test"
    assert persisted.config_revision == 2
    assert persisted.status == "unchecked"
    assert persisted.egress_ip is None


async def test_older_probe_cannot_overwrite_newer_runtime_failure(
    session: AsyncSession,
):
    route = await _create_online_route(session, name="Ordered health failure")
    proxy = browser_proxy_from_route(route)
    old_probe_started = datetime.now(timezone.utc) - timedelta(seconds=5)

    assert await mark_proxy_route_offline(session, proxy)
    assert not await publish_proxy_probe_result(
        session,
        route_id=route.id,
        tested_revision=route.config_revision,
        result=ProxyProbeResult(
            status="online",
            checked_at=old_probe_started,
            egress_ip="203.0.113.91",
            latency_ms=10,
        ),
    )
    await session.commit()
    await session.refresh(route)

    assert route.status == "offline"
    assert route.last_error == "runtime_proxy_unavailable"


async def test_older_failed_probe_cannot_overwrite_newer_success(
    session: AsyncSession,
):
    route = await _create_online_route(session, name="Ordered health success")
    newer = datetime.now(timezone.utc) + timedelta(seconds=5)
    older = newer - timedelta(seconds=2)
    assert await publish_proxy_probe_result(
        session,
        route_id=route.id,
        tested_revision=route.config_revision,
        result=ProxyProbeResult(
            status="online",
            checked_at=newer,
            egress_ip="203.0.113.92",
            latency_ms=11,
        ),
    )
    assert not await publish_proxy_probe_result(
        session,
        route_id=route.id,
        tested_revision=route.config_revision,
        result=ProxyProbeResult(
            status="offline",
            checked_at=older,
            error="proxy_unavailable",
        ),
    )
    await session.commit()
    await session.refresh(route)

    assert route.status == "online"
    assert route.egress_ip == "203.0.113.92"


async def test_old_transport_failure_cannot_poison_new_route_revision(
    session: AsyncSession,
):
    route = await _create_online_route(session, name="Revision health")
    old_proxy = browser_proxy_from_route(route)
    route.config_revision += 1
    route.host = "new-proxy.example.test"
    route.last_checked_at = datetime.now(timezone.utc)
    await session.commit()

    assert not await mark_proxy_route_offline(session, old_proxy)
    await session.refresh(route)
    assert route.status == "online"


async def test_database_allows_only_one_home_relay(
    session: AsyncSession,
):
    session.add_all(
        [
            ProxyRoute(
                name="Home A",
                mode="home_relay",
                proxy_type="socks5",
                host="home-relay",
                port=1080,
            ),
            ProxyRoute(
                name="Home B",
                mode="home_relay",
                proxy_type="socks5",
                host="home-relay",
                port=1080,
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.parametrize(
    "payload",
    [
        {
            "name": "Managed bypass",
            "mode": "home_relay",
            "proxy_type": "socks5",
            "host": "attacker.invalid",
            "port": 1080,
        },
        {
            "name": "SOCKS auth",
            "mode": "custom_proxy",
            "proxy_type": "socks5",
            "host": "proxy.example.test",
            "port": 1080,
            "username": "user",
            "password": "password",
        },
        {
            "name": "URL host",
            "mode": "custom_proxy",
            "proxy_type": "https",
            "host": "https://proxy.example.test/path",
            "port": 443,
        },
    ],
)
async def test_proxy_create_rejects_unsafe_or_unsupported_config(
    auth_client: AsyncClient,
    payload: dict,
):
    response = await auth_client.post("/api/proxy-routes", json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", None),
        ("proxy_type", None),
        ("host", None),
        ("port", None),
        ("name", "   "),
        ("host", "   "),
    ],
)
async def test_proxy_update_rejects_null_or_blank_required_fields(
    auth_client: AsyncClient,
    session: AsyncSession,
    field: str,
    value,
):
    session.add(SellerSettings(id=1))
    route = await _create_online_route(session, name=f"Invalid {field}")

    response = await auth_client.patch(
        f"/api/proxy-routes/{route.id}",
        json={field: value},
    )

    assert response.status_code == 422
    await session.refresh(route)
    assert route.name == f"Invalid {field}"
    assert route.host == "proxy.example.test"
    assert route.port == 3128
    assert route.proxy_type == "http"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "name": "   ",
            "mode": "custom_proxy",
            "proxy_type": "http",
            "host": "proxy.example.test",
            "port": 8080,
        },
        {
            "name": "Blank host",
            "mode": "custom_proxy",
            "proxy_type": "http",
            "host": "   ",
            "port": 8080,
        },
    ],
)
async def test_proxy_create_rejects_blank_required_fields(
    auth_client: AsyncClient,
    payload: dict,
):
    response = await auth_client.post("/api/proxy-routes", json=payload)

    assert response.status_code == 422


async def test_home_relay_setup_rejects_blank_name(
    auth_client: AsyncClient,
):
    response = await auth_client.post(
        "/api/proxy-routes/home-relay/setup",
        json={"name": "   ", "autostart": True},
    )

    assert response.status_code == 422


async def test_route_must_be_tested_before_default_or_account_assignment(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    session.add(SellerSettings(id=1))
    await session.commit()
    created = await auth_client.post(
        "/api/proxy-routes",
        json={
            "name": "Test first",
            "mode": "custom_proxy",
            "proxy_type": "https",
            "host": "proxy.example.test",
            "port": 443,
        },
    )
    route_id = created.json()["id"]

    not_default = await auth_client.put(
        "/api/proxy-routes/default", json={"route_id": route_id}
    )
    assert not_default.status_code == 409
    not_assigned = await auth_client.post(
        "/api/accounts",
        json={
            "login": "proxy-check@example.com",
            "password": "password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "proxy_route_id": route_id,
        },
    )
    assert not_assigned.status_code == 422

    async def successful_probe(_route):
        return ProxyProbeResult(
            status="online",
            checked_at=datetime.now(timezone.utc),
            egress_ip="203.0.113.25",
            latency_ms=42,
        )

    monkeypatch.setattr(
        "app.api.routers.proxy_routes.probe_proxy_route", successful_probe
    )
    tested = await auth_client.post(f"/api/proxy-routes/{route_id}/test")
    assert tested.status_code == 200
    assert tested.json()["status"] == "online"
    assert tested.json()["egress_ip"] == "203.0.113.25"

    made_default = await auth_client.put(
        "/api/proxy-routes/default", json={"route_id": route_id}
    )
    assert made_default.status_code == 200
    assert made_default.json()["default_route_id"] == route_id
    direct = await auth_client.put(
        "/api/proxy-routes/default", json={"route_id": None}
    )
    assert direct.status_code == 200
    assert direct.json()["default_route_id"] is None


async def test_proxy_health_mutations_request_capacity_reconcile(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    route = await _create_online_route(session, name="Capacity callback")
    lifecycle = MagicMock()
    app.state.lifecycle = lifecycle

    async def offline_probe(_route):
        return ProxyProbeResult(
            status="offline",
            checked_at=datetime.now(timezone.utc),
            error="proxy_unavailable",
        )

    monkeypatch.setattr(
        "app.api.routers.proxy_routes.probe_proxy_route",
        offline_probe,
    )
    try:
        tested = await auth_client.post(f"/api/proxy-routes/{route.id}/test")
        disabled = await auth_client.patch(
            f"/api/proxy-routes/{route.id}",
            json={"enabled": False},
        )
    finally:
        del app.state.lifecycle

    assert tested.status_code == 200
    assert tested.json()["status"] == "offline"
    assert disabled.status_code == 200
    assert lifecycle.request_capacity_reconcile.call_count == 2


async def test_default_route_change_requests_capacity_reconcile(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    session.add(SellerSettings(id=1))
    route = await _create_online_route(session, name="Default capacity")
    lifecycle = MagicMock()
    app.state.lifecycle = lifecycle
    try:
        response = await auth_client.put(
            "/api/proxy-routes/default",
            json={"route_id": route.id},
        )
    finally:
        del app.state.lifecycle

    assert response.status_code == 200
    lifecycle.request_capacity_reconcile.assert_called_once_with()


async def test_delete_referenced_route_is_blocked_without_silent_fallback(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    session.add(SellerSettings(id=1))
    route = await _create_online_route(session)
    account = Account(
        login="bound@example.com",
        password_encrypted="password",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        proxy_route_id=route.id,
    )
    session.add(account)
    await session.commit()
    account_id = account.id
    route_id = route.id

    response = await auth_client.delete(f"/api/proxy-routes/{route_id}")

    assert response.status_code == 409
    session.expire_all()
    persisted = await session.get(Account, account_id)
    assert persisted is not None and persisted.proxy_route_id == route_id


async def test_delete_reference_race_returns_conflict_and_fails_closed(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
):
    session.add(SellerSettings(id=1))
    route = await _create_online_route(session, name="Delete race")
    route_id = route.id
    original_commit = session.commit
    calls = 0

    async def commit_with_reference_race():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise IntegrityError(
                "DELETE FROM proxy_routes",
                {"id": route_id},
                RuntimeError("foreign key race"),
            )
        await original_commit()

    monkeypatch.setattr(session, "commit", commit_with_reference_race)
    response = await auth_client.delete(f"/api/proxy-routes/{route_id}")

    assert response.status_code == 409
    session.expire_all()
    persisted = await session.get(ProxyRoute, route_id)
    assert persisted is not None
    assert persisted.status == "offline"
    assert persisted.last_error == "proxy_route_reference_changed"
    assert persisted.config_revision == 2


async def test_resolver_fails_closed_for_disabled_configured_route(
    session: AsyncSession,
):
    route = ProxyRoute(
        name="Disabled",
        mode="custom_proxy",
        proxy_type="http",
        host="proxy.example.test",
        port=3128,
        enabled=False,
    )
    session.add(route)
    await session.flush()
    session.add(SellerSettings(id=1, default_proxy_route_id=route.id))
    account = Account(
        login="fail-closed@example.com",
        password_encrypted="password",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
    )
    session.add(account)
    await session.commit()

    with pytest.raises(ProxyUnavailableError):
        await resolve_browser_proxy(session, account)


async def test_resolver_fails_closed_for_stale_online_route(
    session: AsyncSession,
):
    route = ProxyRoute(
        name="Stale online",
        mode="custom_proxy",
        proxy_type="http",
        host="proxy.example.test",
        port=3128,
        enabled=True,
        status="online",
        last_checked_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    session.add(route)
    await session.flush()
    session.add(SellerSettings(id=1, default_proxy_route_id=route.id))
    account = Account(
        login="stale-route@example.com",
        password_encrypted="password",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
    )
    session.add(account)
    await session.commit()

    with pytest.raises(ProxyUnavailableError):
        await resolve_browser_proxy(session, account)

    route.last_checked_at = datetime.now(timezone.utc)
    await session.commit()
    assert (await resolve_browser_proxy(session, account)).route_id == route.id


async def test_route_list_presents_stale_online_state_as_offline(
    auth_client: AsyncClient,
    session: AsyncSession,
):
    route = await _create_online_route(session, name="Stale presentation")
    route.last_checked_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.commit()

    response = await auth_client.get("/api/proxy-routes")

    assert response.status_code == 200
    item = next(entry for entry in response.json()["routes"] if entry["id"] == route.id)
    assert item["status"] == "offline"
    assert item["last_error"] == "proxy_check_stale"


@pytest.mark.parametrize("status", ["unchecked", "offline"])
async def test_resolver_requires_online_status_but_probe_builder_does_not(
    session: AsyncSession,
    status: str,
):
    route = ProxyRoute(
        name=f"Status {status}",
        mode="custom_proxy",
        proxy_type="http",
        host="proxy.example.test",
        port=3128,
        enabled=True,
        status=status,
    )
    session.add(route)
    await session.flush()
    session.add(SellerSettings(id=1, default_proxy_route_id=route.id))
    account = Account(
        login=f"{status}@example.com",
        password_encrypted="password",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
    )
    session.add(account)
    await session.commit()

    with pytest.raises(ProxyUnavailableError):
        await resolve_browser_proxy(session, account)
    assert browser_proxy_from_route(route).route_id == route.id


def test_account_and_default_route_foreign_keys_restrict_deletion():
    account_fk = next(
        iter(Account.__table__.c.proxy_route_id.foreign_keys)
    )
    settings_fk = next(
        iter(SellerSettings.__table__.c.default_proxy_route_id.foreign_keys)
    )

    assert account_fk.ondelete == "RESTRICT"
    assert settings_fk.ondelete == "RESTRICT"


async def test_validation_reports_proxy_unavailable_and_passes_route_to_openai(
    session: AsyncSession,
    monkeypatch,
):
    route = await _create_online_route(session, name="OpenAI proxy")
    session.add(SellerSettings(id=1, default_proxy_route_id=route.id))
    account = Account(
        login="openai-proxy@example.com",
        password_encrypted="password",
        totp_secret_encrypted="JBSWY3DPEHPK3PXP",
        status="pending_validation",
    )
    session.add(account)
    await session.commit()
    observed: list[BrowserProxy | None] = []

    @asynccontextmanager
    async def unavailable_browser(*_args, proxy=None, **_kwargs):
        observed.append(proxy)
        raise ProxyUnavailableError()
        yield  # pragma: no cover

    monkeypatch.setattr(
        "app.services.account_validation.browser_context", unavailable_browser
    )

    with pytest.raises(AccountValidationError) as error:
        await validate_account(session, account.id)

    assert error.value.code == "proxy_unavailable"
    assert error.value.stage == "proxy"
    assert observed and observed[0] is not None
    assert observed[0].route_id == route.id


async def test_outlook_browser_uses_same_playwright_proxy(monkeypatch):
    from app.integrations.email.outlook_web_provider import OutlookWebProvider
    import app.integrations.email.outlook_web_provider as outlook_module

    proxy = BrowserProxy(
        route_id=7,
        proxy_type="http",
        host="proxy.example.test",
        port=3128,
        username="user",
        password="password",
    )
    page = SimpleNamespace(set_default_timeout=lambda _timeout: None)
    context = SimpleNamespace(
        new_page=AsyncMock(return_value=page),
        close=AsyncMock(),
    )
    browser = SimpleNamespace(
        new_context=AsyncMock(return_value=context),
        close=AsyncMock(),
    )
    chromium = SimpleNamespace(launch=AsyncMock(return_value=browser))
    playwright = SimpleNamespace(chromium=chromium)

    @asynccontextmanager
    async def fake_async_playwright():
        yield playwright

    monkeypatch.setattr(outlook_module, "async_playwright", fake_async_playwright)
    provider = OutlookWebProvider("user@hotmail.com", "password", proxy=proxy)
    monkeypatch.setattr(provider, "_open_mailbox", AsyncMock())

    async with provider._mailbox_session() as (_page, _context):
        assert _page is page

    assert chromium.launch.await_args.kwargs["proxy"] == {
        "server": "http://proxy.example.test:3128",
        "username": "user",
        "password": "password",
    }


async def test_manual_setup_uses_runtime_privilege_for_staging(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
    tmp_path: Path,
):
    _configure_home_relay(monkeypatch, tmp_path)
    monkeypatch.setattr(
        proxy_service,
        "_wait_for_session_generation_ack",
        _ack_session_rotation,
    )
    session.add(SellerSettings(id=1))
    await session.commit()

    setup = await auth_client.post(
        "/api/proxy-routes/home-relay/setup",
        json={"name": "Manual home", "autostart": False},
    )
    assert setup.status_code == 201
    command = setup.json()["powershell_command"]
    protected_body = _decoded_protected_setup_body(command)

    assert "$isAdministrator" in command
    assert "if ($isAdministrator)" in command
    assert "[Environment+SpecialFolder]::LocalApplicationData" in command
    assert "$env:LOCALAPPDATA" not in command
    # The non-admin PS7 path retains its trusted module search path because
    # Install.ps1 may need ScheduledTasks after a later elevated retry.
    local_branch = command.split(" } else { ", 1)[1]
    assert "$env:PSModulePath" not in local_branch
    assert "-DisableAutoStart" in command
    assert "[Environment+SpecialFolder]::CommonApplicationData" in (
        protected_body
    )
    assert "$env:ProgramData" not in protected_body
    assert "$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')" in (
        protected_body
    )
    assert "Documents\\WindowsPowerShell" not in protected_body
    assert protected_body.index("$env:PSModulePath") < protected_body.index(
        "Expand-Archive"
    )
    assert "[IO.Directory]::CreateDirectory($stagePath, $stageAcl)" in (
        protected_body
    )
    assert "-DisableAutoStart" in protected_body

    repaired = await auth_client.post(
        "/api/proxy-routes/home-relay/setup",
        json={"name": "Manual home", "autostart": False},
    )
    assert repaired.status_code == 201
    routes = (await auth_client.get("/api/proxy-routes")).json()["routes"]
    assert routes[0]["config_revision"] == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5.1 only")
async def test_generated_commands_parse_in_windows_powershell_51(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
    tmp_path: Path,
):
    _configure_home_relay(monkeypatch, tmp_path)
    monkeypatch.setattr(
        proxy_service,
        "_wait_for_session_generation_ack",
        _ack_session_rotation,
    )
    session.add(SellerSettings(id=1))
    await session.commit()

    commands: list[str] = []
    for autostart in (True, False):
        setup = await auth_client.post(
            "/api/proxy-routes/home-relay/setup",
            json={
                "name": "PowerShell parser",
                "autostart": autostart,
            },
        )
        assert setup.status_code == 201
        commands.append(setup.json()["powershell_command"])

    for command in commands:
        _assert_windows_powershell_51_parses(command)
        _assert_windows_powershell_51_parses(
            _decoded_protected_setup_body(command)
        )
    routes = (await auth_client.get("/api/proxy-routes")).json()["routes"]
    assert routes[0]["config_revision"] == 2

    executable = (
        Path(os.environ["WINDIR"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    overload_check = subprocess.run(
        [
            str(executable),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$types = [Type[]]@([string], "
            "[Security.AccessControl.DirectorySecurity]); "
            "if ($null -eq [IO.Directory].GetMethod("
            "'CreateDirectory', $types)) { exit 1 }",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert overload_check.returncode == 0, (
        overload_check.stderr or overload_check.stdout
    )


async def test_home_relay_flows_use_consistent_database_lock_order(
    auth_client: AsyncClient,
    session: AsyncSession,
    test_engine,
    monkeypatch,
    tmp_path: Path,
):
    key_material, _authorized_keys_path = _configure_home_relay(
        monkeypatch, tmp_path
    )
    monkeypatch.setattr(
        proxy_service,
        "_wait_for_session_generation_ack",
        _ack_session_rotation,
    )
    session.add(SellerSettings(id=1))
    await session.commit()
    locked_tables: list[str] = []

    def record_for_update(
        _connection,
        clause,
        _multiparams,
        _params,
        _execution_options,
    ):
        if getattr(clause, "_for_update_arg", None) is None:
            return
        table_names = {
            table.name
            for table in clause.get_final_froms()
            if getattr(table, "name", None)
        }
        if "proxy_routes" in table_names:
            locked_tables.append("proxy_routes")
        elif "seller_settings" in table_names:
            locked_tables.append("seller_settings")
        elif "home_relay_setups" in table_names:
            locked_tables.append("home_relay_setups")

    event.listen(test_engine.sync_engine, "before_execute", record_for_update)
    try:
        first = await auth_client.post(
            "/api/proxy-routes/home-relay/setup",
            json={"name": "Lock order", "autostart": False},
        )
        assert first.status_code == 201

        locked_tables.clear()
        second = await auth_client.post(
            "/api/proxy-routes/home-relay/setup",
            json={"name": "Lock order", "autostart": False},
        )
        assert second.status_code == 201
        assert locked_tables[:3] == [
            "proxy_routes",
            "seller_settings",
            "home_relay_setups",
        ]

        locked_tables.clear()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as public_client:
            enrolled = await public_client.post(
                "/api/proxy-routes/home-relay/enroll",
                headers={
                    "Authorization": f"Bearer {second.json()['setup_token']}"
                },
                json={
                    "schema_version": 1,
                    "machine_name": "WORKSTATION",
                    "display_name": "Lock order",
                    "public_key": f"ssh-ed25519 {key_material}",
                    "client_version": "1.0.0",
                },
            )
        assert enrolled.status_code == 200
        assert locked_tables[:2] == ["proxy_routes", "home_relay_setups"]

        route_id = (
            await auth_client.get("/api/proxy-routes")
        ).json()["routes"][0]["id"]
        locked_tables.clear()
        deleted = await auth_client.delete(f"/api/proxy-routes/{route_id}")
        assert deleted.status_code == 204
        assert locked_tables[:3] == [
            "proxy_routes",
            "seller_settings",
            "home_relay_setups",
        ]
    finally:
        event.remove(
            test_engine.sync_engine,
            "before_execute",
            record_for_update,
        )


async def test_home_relay_pairing_is_one_time_and_revokes_key_on_delete(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
    tmp_path: Path,
):
    from app.config import get_settings

    key_material, authorized_keys_path = _configure_home_relay(
        monkeypatch, tmp_path
    )
    monkeypatch.setattr(
        proxy_service,
        "_wait_for_session_generation_ack",
        _ack_session_rotation,
    )
    session.add(SellerSettings(id=1))
    await session.commit()

    setup = await auth_client.post(
        "/api/proxy-routes/home-relay/setup",
        json={"name": "Home PC", "autostart": True},
    )
    assert setup.status_code == 201
    assert setup.headers["cache-control"] == "no-store"
    setup_data = setup.json()
    token = setup_data["setup_token"]
    assert token not in setup_data["script_download_url"]
    assert "installer.zip" in setup_data["script_download_url"]
    command = setup_data["powershell_command"]
    protected_body = _decoded_protected_setup_body(command)
    assert "Open PowerShell using Run as administrator" in command
    assert "WindowsPowerShell\\v1.0\\powershell.exe" in command
    assert "$env:WINDIR" not in command
    assert "-EnableAutoStart" in protected_body
    assert "Get-FileHash" in protected_body
    assert setup_data["installer_sha256"] in protected_body
    assert "[Environment+SpecialFolder]::CommonApplicationData" in (
        protected_body
    )
    assert "$env:ProgramData" not in protected_body
    assert "$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')" in (
        protected_body
    )
    assert "[Guid]::NewGuid()" in protected_body
    assert "[IO.Directory]::CreateDirectory($stagePath, $stageAcl)" in (
        protected_body
    )
    assert "O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)" in protected_body
    assert protected_body.index("CreateDirectory") < protected_body.index(
        "Invoke-WebRequest"
    )
    assert "finally" in protected_body
    assert "funpay-home-relay.zip" not in protected_body

    routes_before_enrollment = (await auth_client.get("/api/proxy-routes")).json()
    assert routes_before_enrollment["routes"][0]["config_revision"] == 1

    public_client = AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    )
    async with public_client:
        installer = await public_client.get(
            "/api/proxy-routes/home-relay/installer.zip"
        )
        assert installer.status_code == 200
        assert hashlib.sha256(installer.content).hexdigest() == (
            setup_data["installer_sha256"]
        )
        enrollment = await public_client.post(
            "/api/proxy-routes/home-relay/enroll",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "schema_version": 1,
                "machine_name": "WORKSTATION",
                "display_name": "Home PC",
                "public_key": f"ssh-ed25519 {key_material} test",
                "client_version": "1.0.0",
            },
        )
        replay = await public_client.post(
            "/api/proxy-routes/home-relay/enroll",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "schema_version": 1,
                "machine_name": "WORKSTATION",
                "display_name": "Home PC",
                "public_key": f"ssh-ed25519 {key_material}",
                "client_version": "1.0.0",
            },
        )

    assert enrollment.status_code == 200
    assert enrollment.headers["cache-control"] == "no-store"
    assert enrollment.json()["relay_id"].startswith("relay-")
    assert enrollment.json()["host_key"] == {
        "type": "ssh-ed25519",
        "data": key_material,
    }
    assert replay.status_code == 401
    authorized = authorized_keys_path.read_text(encoding="utf-8")
    assert "restrict,port-forwarding" in authorized
    assert token not in authorized

    routes = (await auth_client.get("/api/proxy-routes")).json()
    assert routes["default_route_id"] is None
    assert routes["routes"][0]["config_revision"] == 2
    route_id = routes["routes"][0]["id"]

    generation_before_repair = (
        tmp_path / "session_generation"
    ).read_text(encoding="utf-8").strip()
    repair = await auth_client.post(
        "/api/proxy-routes/home-relay/setup",
        json={"name": "Home PC", "autostart": True},
    )
    assert repair.status_code == 201
    assert not authorized_keys_path.read_text(encoding="utf-8")
    generation_after_repair = (
        tmp_path / "session_generation"
    ).read_text(encoding="utf-8").strip()
    assert generation_after_repair != generation_before_repair
    assert (
        tmp_path / "session_generation.ack"
    ).read_text(encoding="utf-8").strip() == generation_after_repair
    repaired_routes = (await auth_client.get("/api/proxy-routes")).json()
    assert repaired_routes["routes"][0]["config_revision"] == 3

    deleted = await auth_client.delete(f"/api/proxy-routes/{route_id}")
    assert deleted.status_code == 204
    assert not authorized_keys_path.read_text(encoding="utf-8")
    assert (
        tmp_path / "session_generation.ack"
    ).read_text(encoding="utf-8").strip() == (
        tmp_path / "session_generation"
    ).read_text(encoding="utf-8").strip()
    get_settings.cache_clear()


async def test_session_generation_requires_exact_ack(
    monkeypatch,
    tmp_path: Path,
):
    from app.config import get_settings

    monkeypatch.setenv("HOME_RELAY_SESSION_ACK_TIMEOUT_SECONDS", "1")
    get_settings.cache_clear()
    authorized_keys_path = tmp_path / "authorized_keys"
    rotation = asyncio.create_task(
        proxy_service._rotate_relay_sessions(authorized_keys_path)
    )
    generation_path = tmp_path / "session_generation"
    for _ in range(100):
        if generation_path.exists():
            break
        await asyncio.sleep(0.01)
    assert generation_path.exists()
    generation = generation_path.read_text(encoding="utf-8").strip()
    assert generation

    ack_path = tmp_path / "session_generation.ack"
    ack_path.write_text(f"{generation}-wrong\n", encoding="utf-8")
    await asyncio.sleep(0.06)
    assert not rotation.done()
    ack_path.write_text(generation + "\n", encoding="utf-8")
    await rotation
    get_settings.cache_clear()


async def test_new_key_is_added_only_after_stale_sessions_are_acknowledged(
    monkeypatch,
    tmp_path: Path,
):
    key_material, authorized_keys_path = _configure_home_relay(
        monkeypatch, tmp_path
    )
    unrelated = f"ssh-ed25519 {key_material} operator-key"
    stale = (
        "restrict,port-forwarding,permitlisten=\"0.0.0.0:1080\" "
        f"ssh-ed25519 {key_material} funpay-relay-999-stale"
    )
    authorized_keys_path.write_text(
        unrelated + "\n" + stale + "\n",
        encoding="utf-8",
    )
    observed_before_ack: list[str] = []

    async def acknowledge_after_inspection(
        ack_path: Path,
        generation: str,
        *,
        timeout: float,
    ) -> None:
        del timeout
        current = authorized_keys_path.read_text(encoding="utf-8")
        observed_before_ack.append(current)
        assert "funpay-relay-" not in current
        assert unrelated in current
        ack_path.write_text(generation + "\n", encoding="utf-8")

    monkeypatch.setattr(
        proxy_service,
        "_wait_for_session_generation_ack",
        acknowledge_after_inspection,
    )
    public_key, fingerprint = proxy_service.validate_public_key(
        f"ssh-ed25519 {key_material}"
    )

    await proxy_service.install_authorized_key(
        route_id=1,
        public_key=public_key,
        fingerprint=fingerprint,
    )

    assert observed_before_ack
    installed = authorized_keys_path.read_text(encoding="utf-8")
    assert unrelated in installed
    assert "funpay-relay-999-stale" not in installed
    assert "funpay-relay-1-" in installed


async def test_enrollment_commit_failure_revokes_installed_key(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
    tmp_path: Path,
):
    key_material, authorized_keys_path = _configure_home_relay(
        monkeypatch, tmp_path
    )
    monkeypatch.setattr(
        proxy_service,
        "_wait_for_session_generation_ack",
        _ack_session_rotation,
    )
    session.add(SellerSettings(id=1))
    await session.commit()
    setup = await auth_client.post(
        "/api/proxy-routes/home-relay/setup",
        json={"name": "Commit compensation", "autostart": False},
    )
    token = setup.json()["setup_token"]
    original_commit = session.commit
    fail_next_commit = True

    async def fail_enrollment_commit_once():
        nonlocal fail_next_commit
        if fail_next_commit:
            fail_next_commit = False
            raise IntegrityError(
                "UPDATE home_relay_setups",
                {},
                RuntimeError("simulated commit failure"),
            )
        await original_commit()

    monkeypatch.setattr(session, "commit", fail_enrollment_commit_once)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as public_client:
        response = await public_client.post(
            "/api/proxy-routes/home-relay/enroll",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "schema_version": 1,
                "machine_name": "WORKSTATION",
                "display_name": "Commit compensation",
                "public_key": f"ssh-ed25519 {key_material}",
                "client_version": "1.0.0",
            },
        )

    assert response.status_code == 500
    assert not authorized_keys_path.read_text(encoding="utf-8")
    session.expire_all()
    setup_row = (await session.execute(select(HomeRelaySetup))).scalar_one()
    persisted = await session.get(ProxyRoute, setup_row.route_id)
    assert setup_row.consumed_at is None
    assert persisted is not None
    assert persisted.enabled is False
    assert persisted.status == "offline"
    assert persisted.last_error == "enrollment_commit_failed"


async def test_enrollment_and_delete_fail_closed_without_session_ack(
    auth_client: AsyncClient,
    session: AsyncSession,
    monkeypatch,
    tmp_path: Path,
):
    key_material, authorized_keys_path = _configure_home_relay(
        monkeypatch, tmp_path
    )
    session.add(SellerSettings(id=1))
    await session.commit()
    setup = await auth_client.post(
        "/api/proxy-routes/home-relay/setup",
        json={"name": "Home timeout", "autostart": False},
    )
    assert setup.status_code == 201
    token = setup.json()["setup_token"]
    enrollment_payload = {
        "schema_version": 1,
        "machine_name": "WORKSTATION",
        "display_name": "Home timeout",
        "public_key": f"ssh-ed25519 {key_material}",
        "client_version": "1.0.0",
    }

    async def reject_rotation(
        ack_path: Path,
        generation: str,
        *,
        timeout: float,
    ) -> None:
        del ack_path, generation, timeout
        raise OSError("home_relay_session_ack_timeout")

    monkeypatch.setattr(
        proxy_service,
        "_wait_for_session_generation_ack",
        reject_rotation,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as public_client:
        failed = await public_client.post(
            "/api/proxy-routes/home-relay/enroll",
            headers={"Authorization": f"Bearer {token}"},
            json=enrollment_payload,
        )
    assert failed.status_code == 503
    assert not authorized_keys_path.read_text(encoding="utf-8")

    setup_row = (await session.execute(select(HomeRelaySetup))).scalar_one()
    route = await session.get(ProxyRoute, setup_row.route_id)
    assert setup_row.consumed_at is None
    assert route is not None
    assert route.enabled is False
    assert route.status == "offline"
    assert route.last_error == "relay_session_ack_timeout"
    failed_revision = route.config_revision

    monkeypatch.setattr(
        proxy_service,
        "_wait_for_session_generation_ack",
        _ack_session_rotation,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as public_client:
        retried = await public_client.post(
            "/api/proxy-routes/home-relay/enroll",
            headers={"Authorization": f"Bearer {token}"},
            json=enrollment_payload,
        )
    assert retried.status_code == 200
    await session.refresh(setup_row)
    await session.refresh(route)
    assert setup_row.consumed_at is not None
    assert route.enabled is True
    assert route.status == "unchecked"
    assert route.config_revision == failed_revision + 1

    monkeypatch.setattr(
        proxy_service,
        "_wait_for_session_generation_ack",
        reject_rotation,
    )
    failed_repair = await auth_client.post(
        "/api/proxy-routes/home-relay/setup",
        json={"name": "Home timeout", "autostart": False},
    )
    assert failed_repair.status_code == 503
    await session.refresh(route)
    assert route.enabled is False
    assert route.status == "offline"
    assert route.last_error == "relay_session_ack_timeout"
    assert not authorized_keys_path.read_text(encoding="utf-8")

    deleted = await auth_client.delete(f"/api/proxy-routes/{route.id}")
    assert deleted.status_code == 503
    await session.refresh(route)
    assert route.enabled is False
    assert route.status == "offline"
    assert route.last_error == "relay_session_ack_timeout"
    assert not authorized_keys_path.read_text(encoding="utf-8")
