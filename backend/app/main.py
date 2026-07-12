from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routers.auth import router as auth_router
from app.db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Корректно освобождаем пул соединений при остановке приложения
    await engine.dispose()


app = FastAPI(title="FunPay ChatGPT Rental Bot", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
