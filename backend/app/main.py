"""FastAPI 应用：骨架期只有 /health 与 GET /api/tasks/{id}（轮询语义的种子端点）。"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import db
from .models import Task


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
    def get_task(task_id: int) -> Task:
        with db.SessionLocal() as session:
            task = session.get(Task, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task

    return app


app = create_app()
