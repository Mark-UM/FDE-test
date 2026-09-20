import asyncio

from httpx import ASGITransport, AsyncClient

from app.main import app


def test_health() -> None:
    async def request_health():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get("/health")

    response = asyncio.run(request_health())

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "ecommerce-order-support-backend",
    }
