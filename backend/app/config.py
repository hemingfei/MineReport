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

    # 认证
    invitation_ttl_days: int = 7  # 邀请码有效期
    session_ttl_days: int = 14  # 会话有效期（固定，不滑动续期）
    session_cookie_secure: bool = False  # 生产经 HTTPS 反代时置 true
    bootstrap_admin_email: str = ""  # 初始管理员引导（库中无 admin 时创建一次）
    bootstrap_admin_password: str = ""

    # 研报入库与转换（#13）
    upload_max_mb: int = 50  # 上传文件大小上限
    allow_reader_download: bool = False  # 读者原始文件下载（默认拒，可放开）
    report_soft_delete_days: int = 30  # 软删除恢复窗口（天）
    scan_min_chars_per_page: int = 30  # 每页平均字符数低于此值判为扫描版（markitdown 静默空串防护）
    markdown_min_chars: int = 200  # 转换输出字符数闸门（清洗前口径）

    # LLM 分析管道（#15）。凭据仅环境变量（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL），源码无字面量
    llm_base_url: str = ""  # OpenAI 兼容端点（含版本路径，如 https://host/v1）
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 300.0  # 整篇单次调用深度报告实测 52~103s，留足余量
    analysis_max_input_chars: int = 50_000  # 超过则降级分块兜底（spec 阈值）
    analysis_chunk_chars: int = 26_000  # 分块大小（中文约 1 字 = 1 token）
    analysis_chunk_overlap: int = 1_000
    analysis_head_chars: int = 3_000  # publish_date 正则锚定的"首页"窗口

    # 综合分析（#20）。输入按发布日期取最近 N 篇（token 闸门），超出截断并记元信息
    synthesis_max_reports: int = 60

    # 连接器与订阅调度（#19）。凭据仅环境变量（FXBAOGAO_API_KEY），源码无字面量
    fxbaogao_api_key: str = ""
    fxbaogao_api_base: str = "https://api.fxbaogao.com"
    fxbaogao_download_base: str = "https://dr.fxbaogao.com/"  # download 端点返回相对路径时的拼接前缀
    fxbaogao_timeout_seconds: float = 60.0
    connector_rate_per_second: float = 1.0  # 连接器全局限速（spec：1 req/s）
    connector_max_pages_per_query: int = 3  # search 单查询翻页上限（API 硬上限 10 页）
    subscription_tick_seconds: float = 60.0  # worker 里 APScheduler 扫 due 订阅的间隔
    subscription_default_interval_hours: int = 6  # 默认订阅间隔
    subscription_jitter_seconds: float = 600.0  # 排程错峰抖动幅度（±，秒）


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
