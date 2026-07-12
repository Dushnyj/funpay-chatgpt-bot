from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.integrations.openai.oauth import OPENAI_CLIENT_ID, OPENAI_ISSUER, RefreshedTokens, exchange_code_for_tokens


DEVICE_AUTH_REDIRECT_URI = f"{OPENAI_ISSUER}/deviceauth/callback"
DEVICE_AUTH_VERIFICATION_URL = f"{OPENAI_ISSUER}/codex/device"


class DeviceAuthError(RuntimeError):
    """A safe device-authorization error that never contains tokens or codes."""


@dataclass(frozen=True)
class DeviceCode:
    device_auth_id: str
    user_code: str
    interval_seconds: int
    verification_url: str = DEVICE_AUTH_VERIFICATION_URL


@dataclass(frozen=True)
class DeviceAuthorization:
    authorization_code: str
    code_verifier: str
    code_challenge: str


async def request_device_code() -> DeviceCode:
    """Start the same user-assisted device flow supported by OpenAI Codex."""
    url = f"{OPENAI_ISSUER}/api/accounts/deviceauth/usercode"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, json={"client_id": OPENAI_CLIENT_ID})
    if not response.is_success:
        raise DeviceAuthError(f"device_code_request_failed:{response.status_code}")
    try:
        payload = response.json()
        interval = int(str(payload.get("interval", "5")).strip())
        return DeviceCode(
            device_auth_id=str(payload["device_auth_id"]),
            user_code=str(payload.get("user_code") or payload["usercode"]),
            interval_seconds=max(1, interval),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DeviceAuthError("device_code_response_invalid") from exc


async def poll_device_authorization(
    device_auth_id: str,
    user_code: str,
) -> DeviceAuthorization | None:
    """Poll once. ``None`` means the operator has not completed the browser step yet."""
    url = f"{OPENAI_ISSUER}/api/accounts/deviceauth/token"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            url,
            json={"device_auth_id": device_auth_id, "user_code": user_code},
        )
    if response.status_code in (403, 404):
        return None
    if not response.is_success:
        raise DeviceAuthError(f"device_code_poll_failed:{response.status_code}")
    try:
        payload = response.json()
        return DeviceAuthorization(
            authorization_code=str(payload["authorization_code"]),
            code_verifier=str(payload["code_verifier"]),
            code_challenge=str(payload["code_challenge"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DeviceAuthError("device_code_poll_response_invalid") from exc


async def exchange_device_authorization(
    authorization: DeviceAuthorization,
) -> RefreshedTokens:
    return await exchange_code_for_tokens(
        authorization.authorization_code,
        authorization.code_verifier,
        DEVICE_AUTH_REDIRECT_URI,
    )
