"""任务 API：轮询查询（任意登录用户）与失败任务分阶段重试（analyst 起）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session as OrmSession

from ..auth import require_role
from ..db import get_db
from ..models import Role, Task, TaskStatus, User

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskResponse(BaseModel):
    id: int
    kind: str
    status: str
    payload: dict | None
    result: dict | None
    attempts: int

    model_config = {"from_attributes": True}


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: int, user: User = Depends(require_role(Role.READER)), db: OrmSession = Depends(get_db)
) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return task


@router.post("/{task_id}/retry", response_model=TaskResponse)
def retry_task(
    task_id: int,
    user: User = Depends(require_role(Role.ANALYST)),
    db: OrmSession = Depends(get_db),
) -> Task:
    """失败任务重置回 uploaded 重新入队；已完成的阶段由 worker 按 stages_done 跳过。"""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    if task.status != TaskStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"仅失败任务可重试（当前 {task.status}）",
        )
    task.status = TaskStatus.UPLOADED
    task.result = None
    task.claimed_at = None
    db.commit()
    db.refresh(task)
    return task
