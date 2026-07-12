from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient

from app.main import app


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
