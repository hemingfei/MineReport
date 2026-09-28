"""FastAPI 应用：/health、认证、邀请管理与任务轮询。"""

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session as OrmSession

from . import db
from .auth import get_current_user
from .models import Task, User
from .routers import admin, auth


class HealthResponse(BaseModel):
    status: str = "ok"


class TaskResponse(BaseModel):
    id: int
    kind: str
    status: str
    payload: dict | None
    result: dict | None
    attempts: int


def create_app() -> FastAPI:
    app = FastAPI(title="MineReport API")

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/api/tasks/{task_id}", response_model=TaskResponse)
    def get_task(task_id: int, user: User = Depends(get_current_user)) -> Task:
        with db.SessionLocal() as session:
            task = session.get(Task, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task

    app.include_router(auth.router)
    app.include_router(admin.router)

    return app


app = create_app()
