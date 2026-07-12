import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.accounts import router as accounts_router
from app.api.routers.auth import router as auth_router
from app.api.routers.catalog import router as catalog_router
from app.api.routers.chats import router as chats_router
from app.api.routers.lots import router as lots_router
from app.api.routers.orders import router as orders_router
from app.api.routers.rentals import router as rentals_router
from app.api.routers.settings import router as settings_router
from app.api.routers.prices import router as prices_router
from app.api.routers.templates import router as templates_router
from app.api.routers.metrics import router as metrics_router
from app.api.deps import get_db_session
from app.db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.app_lifecycle import AppLifecycle
    from app.config import get_settings
    from app.db.bootstrap import bootstrap_database
    from app.db.migrations import upgrade_database
    from app.db.session import async_session_factory
    from app.services.golden_key import get_effective_funpay_key

    settings = get_settings()
    await upgrade_database(engine)
    await bootstrap_database(settings, async_session_factory)
    async with async_session_factory() as session:
        golden_key = await get_effective_funpay_key(session, settings)
    lifecycle = AppLifecycle(
        golden_key=golden_key,
        category_id=0,
    )
    app.state.lifecycle = lifecycle
    await lifecycle.start()
    yield
    await lifecycle.stop()
    await engine.dispose()


app = FastAPI(title="FunPay ChatGPT Rental Bot", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(catalog_router)
app.include_router(accounts_router)
app.include_router(chats_router)
app.include_router(lots_router)
app.include_router(orders_router)
app.include_router(rentals_router)
app.include_router(settings_router)
app.include_router(prices_router)
app.include_router(templates_router)
app.include_router(metrics_router)


@app.get("/health")
async def health(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        await session.execute(select(1))
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "database": "unavailable"},
        )

    lifecycle = getattr(request.app.state, "lifecycle", None)
    runner = getattr(lifecycle, "runner", None)
    if getattr(lifecycle, "last_funpay_error", None) or getattr(runner, "last_error", None):
        funpay_status = "error"
    elif runner is not None and getattr(runner, "started", False):
        funpay_status = "connected"
    else:
        funpay_status = "disconnected"
    scheduler = getattr(lifecycle, "scheduler", None)
    scheduler_status = "running" if getattr(scheduler, "running", False) else "stopped"
    return {
        "status": "ok",
        "database": "ok",
        "scheduler": scheduler_status,
        "funpay": funpay_status,
    }


# Раздача собранной SPA — ищет frontend/dist относительно CWD или пакета app.
# В Docker CWD=/app → /app/frontend/dist. В dev — относительно репозитория.
_FRONTEND_DIST = os.path.join(os.getcwd(), "frontend", "dist")
if not os.path.isdir(_FRONTEND_DIST):
    _FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist")
_INDEX_HTML = os.path.join(_FRONTEND_DIST, "index.html")
_FRONTEND_ROOT = Path(_FRONTEND_DIST).resolve()

if os.path.isdir(_FRONTEND_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(_FRONTEND_DIST, "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> FileResponse:
        """Serve Vite public assets and fall back only for client-side routes."""
        candidate = (_FRONTEND_ROOT / full_path).resolve()
        if candidate.is_relative_to(_FRONTEND_ROOT) and candidate.is_file():
            return FileResponse(candidate)
        if full_path.startswith("api/") or Path(full_path).suffix:
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(_INDEX_HTML)

