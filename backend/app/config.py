"""应用配置：全部来自环境变量，源码不写任何凭据字面量。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/minereport"

    # 存储根目录（本地卷实现）；容器内挂载卷，测试用 tmp_path
    storage_root: str = "./data/files"

    # worker
    worker_poll_interval: float = 2.0  # 轮询任务表的间隔（秒）
    worker_heartbeat_interval: float = 30.0  # 心跳日志间隔（秒）
    worker_lease_seconds: float = 600.0  # 任务租约：converting 超过此时长视为遗弃、可重新领取
    worker_id: str = "worker-1"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
