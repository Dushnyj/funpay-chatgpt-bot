from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from starlette.responses import HTMLResponse

from app.main import app, browser_security_headers


async def test_health_checks_database_and_runtime_state():
    previous = getattr(app.state, "lifecycle", None)
    app.state.lifecycle = SimpleNamespace(
        runner=SimpleNamespace(started=True, last_error=None),
        scheduler=SimpleNamespace(running=True),
        last_funpay_error=None,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/health")
    finally:
        if previous is None:
            del app.state.lifecycle
        else:
            app.state.lifecycle = previous

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "ok",
        "scheduler": "running",
        "funpay": "connected",
    }
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


async def test_spa_html_is_not_cached_across_deploys():
    request = Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/login",
        "raw_path": b"/login",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 443),
    })

    async def html_response(_request):
        return HTMLResponse("<html></html>")

    response = await browser_security_headers(request, html_response)

    assert response.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    assert response.headers["strict-transport-security"] == "max-age=31536000"
