from httpx import ASGITransport, AsyncClient

from app.main import app


async def test_vite_public_favicon_is_served_as_a_file():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/favicon.ico")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
    assert response.content[:4] == b"\x00\x00\x01\x00"


async def test_spa_route_falls_back_but_unknown_api_does_not():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        spa = await client.get("/chats")
        missing_api = await client.get("/api/does-not-exist")

    assert spa.status_code == 200
    assert "text/html" in spa.headers["content-type"]
    assert missing_api.status_code == 404
    assert missing_api.json()["detail"] == "Not found"
