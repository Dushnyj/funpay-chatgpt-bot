import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routers.accounts import router as accounts_router
from app.api.routers.auth import router as auth_router
from app.api.routers.catalog import router as catalog_router
from app.api.routers.lots import router as lots_router
from app.api.routers.orders import router as orders_router
from app.api.routers.rentals import router as rentals_router
from app.api.routers.settings import router as settings_router
from app.api.routers.prices import router as prices_router
from app.api.routers.templates import router as templates_router
from app.api.routers.metrics import router as metrics_router
from app.db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Корректно освобождаем пул соединений при остановке приложения
    await engine.dispose()


app = FastAPI(title="FunPay ChatGPT Rental Bot", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(catalog_router)
app.include_router(accounts_router)
app.include_router(lots_router)
app.include_router(orders_router)
app.include_router(rentals_router)
app.include_router(settings_router)
app.include_router(prices_router)
app.include_router(templates_router)
app.include_router(metrics_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# Раздача собранной SPA — только если frontend/dist существует.
# Статика (assets) монтируется на /assets, остальные роуты → index.html (SPA fallback).
_FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist")
_INDEX_HTML = os.path.join(_FRONTEND_DIST, "index.html")

if os.path.isdir(_FRONTEND_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(_FRONTEND_DIST, "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> FileResponse:
        """SPA fallback: все неизвестные пути возвращают index.html (клиентский роутинг)."""
        return FileResponse(_INDEX_HTML)

