"""FastAPI application entry point."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import database
from app.config import get_settings
from app.logging_config import configure_logging
from app.routers import (
    accounts,
    auth,
    chat,
    inbox,
    monitor as monitor_router,
    pages,
    proxies,
    templates as templates_router,
    utils,
    voice,
)
from app.services import gateway_pool, monitor as monitor_service
from app.services import topic_listener


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    await database.connect()
    monitor_service.start()
    topic_listener.start()   # real-time MESSAGE_CREATE via gateway
    try:
        yield
    finally:
        await topic_listener.stop()
        await monitor_service.stop()
        await gateway_pool.close_all()
        await database.disconnect()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Discord Farm Manager",
        lifespan=lifespan,
        debug=settings.app_debug,
    )

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        same_site="strict",
        https_only=False,  # local dev; flip to True behind HTTPS
    )

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(accounts.router)
    app.include_router(proxies.router)
    app.include_router(chat.router)
    app.include_router(templates_router.router)
    app.include_router(utils.router)
    app.include_router(monitor_router.router)
    app.include_router(inbox.router)
    app.include_router(voice.router)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.app_host, port=s.app_port, reload=s.app_debug)
