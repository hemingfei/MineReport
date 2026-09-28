"""FastAPI 应用：/health、认证、邀请管理、任务轮询与研报 API。"""

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from .auth import get_current_user
from .models import User
from .routers import admin, auth, reports, tasks


class HealthResponse(BaseModel):
    status: str = "ok"


def create_app() -> FastAPI:
    app = FastAPI(title="MineReport API")

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(reports.router)
    app.include_router(tasks.router)

    return app


app = create_app()
