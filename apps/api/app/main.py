from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__, health
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="HealthSaathi API", version=__version__, openapi_url="/api/v1/openapi.json")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-request-id"],
    )
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router, prefix="/api/v1")
    return app


app = create_app()
