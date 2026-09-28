"""FastAPI 应用：/health、认证、邀请管理、任务轮询与研报 API。"""

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from .auth import get_current_user
from .config import get_settings
from .models import User
from .routers import admin, auth, authors, reports, subscriptions, syntheses, targets, tasks, themes


class HealthResponse(BaseModel):
    status: str = "ok"


class ConfigResponse(BaseModel):
    """前端需要的运行时开关（任意登录用户可见）。"""

    allow_reader_download: bool


def create_app() -> FastAPI:
    app = FastAPI(title="MineReport API")

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/api/config", response_model=ConfigResponse)
    def config(user: User = Depends(get_current_user)) -> ConfigResponse:
        return ConfigResponse(allow_reader_download=get_settings().allow_reader_download)

    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(reports.router)
    app.include_router(targets.router)
    app.include_router(themes.router)
    app.include_router(authors.router)
    app.include_router(subscriptions.router)
    app.include_router(syntheses.router)
    app.include_router(tasks.router)

    return app


app = create_app()
