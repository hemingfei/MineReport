# MineReport

研报挖掘：A股研报挖掘系统。汇聚多来源研报，逐篇 AI 分析（总结、题材、标的、作者、券商），再按题材跨文档综合出共性结论与共识标的。

领域词汇表见 [CONTEXT.md](CONTEXT.md)，技术方案见 [docs/spec/MineReport-spec.md](docs/spec/MineReport-spec.md)。

## 快速启动

依赖：Docker（含 Docker Compose）。

```bash
docker compose up -d --build
```

拉起三个服务：

- **postgres** — PostgreSQL 17 + zhparser 中文分词扩展（镜像 `abcfy2/zhparser:17`），首启自动建 `zhcfg` 全文检索配置
- **api** — FastAPI，`http://localhost:8000`，健康检查 `GET /health`；启动时执行 alembic 迁移
- **worker** — 轮询共享任务表（`tasks`）的分析 worker，日志每 30s 输出一次心跳

验证：

```bash
curl http://localhost:8000/health          # {"status":"ok"}
docker compose logs worker | grep heartbeat # 心跳日志
```

## 后端测试

依赖：本机 Python 3.13+ 与 [uv](https://docs.astral.sh/uv/)，且 compose 的 postgres 正在运行（测试库建在同一实例）。

```bash
cd backend
uv sync
uv run pytest
```

测试基座（spec Testing Decisions）：HTTP API 层为主缝合口，FastAPI TestClient + 真实 Postgres 测试库（`minereport_test`，会话级建库→迁移→跑→清理，可重复执行）。

## 配置

全部配置经环境变量注入（`backend/app/config.py`），凭据不落源码：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/minereport` | 数据库连接串 |
| `STORAGE_ROOT` | `./data/files` | 原始文件存储根（本地卷实现，抽象接口可换 S3/MinIO） |
| `WORKER_ID` / `WORKER_POLL_INTERVAL` / `WORKER_HEARTBEAT_INTERVAL` | `worker-1` / `2.0` / `30.0` | worker 标识与轮询/心跳节奏 |

## 目录结构

```
backend/           FastAPI + worker（共享 Postgres 任务表）
  app/             应用代码（config/db/models/storage/main/worker）
  alembic/         迁移脚本（表结构变更一律走这里）
  postgres/init/   postgres 首启初始化 SQL（zhparser + zhcfg）
  tests/           pytest（真实 PG 测试库）
docker-compose.yml api / worker / postgres 三服务编排
```
