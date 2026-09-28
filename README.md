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
  （`docker-entrypoint-initdb.d` 只在数据卷为空时执行；**既有卷升级到 #18 后须手动补一次**：
  `docker compose exec -T postgres psql -U postgres -d minereport < backend/postgres/init/001_zhparser.sql`，
  否则迁移静默成功但全文搜索写入会报 `text search configuration "zhcfg" does not exist`；随后
  `backend/` 下 `uv run python scripts/backfill_search.py` 回填存量研报的搜索向量）
- **api** — FastAPI，`http://localhost:8000`，健康检查 `GET /health`；启动时执行 alembic 迁移，随后按 `BOOTSTRAP_ADMIN_*` 引导首个管理员（可选）
- **worker** — 轮询共享任务表（`tasks`）的分析 worker，日志每 30s 输出一次心跳

验证：

```bash
curl http://localhost:8000/health          # {"status":"ok"}
docker compose logs worker | grep heartbeat # 心跳日志
```

## 认证（邀请制，三角色）

角色层级 `admin > analyst > reader`，权限依赖为可复用组件（`backend/app/auth.py` 的 `require_role` / `get_current_user`）：

```python
@app.get("/api/...")
def endpoint(user: User = Depends(require_role(Role.ANALYST))): ...  # 至少 analyst，不足 403
```

认证端点：

| 端点 | 说明 |
|---|---|
| `POST /api/auth/register` | 凭邀请码注册（`token`/`email`/`password`），注册即登录 |
| `POST /api/auth/login` / `POST /api/auth/logout` | 会话登录/登出（HttpOnly cookie，服务端会话表） |
| `GET /api/auth/me` | 当前用户 |
| `POST /api/admin/invitations` | 建邀请（`role`，可选 `email` 绑定），仅 admin |
| `GET /api/admin/invitations` | 邀请列表，仅 admin |

首个管理员来自环境变量引导：`.env` 设 `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`（api 容器在库中无 admin 时创建，之后幂等跳过）。后续用户全部走邀请。

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
| `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD` | 空 | 初始管理员引导（库中无 admin 时创建一次） |
| `INVITATION_TTL_DAYS` | `7` | 邀请码有效期（天） |
| `SESSION_TTL_DAYS` / `SESSION_COOKIE_SECURE` | `14` / `false` | 会话有效期与 cookie secure 标记（HTTPS 反代后置 true） |

## 目录结构

```
backend/           FastAPI + worker（共享 Postgres 任务表）
  app/             应用代码（config/db/models/auth/bootstrap/storage/main/worker/routers）
  alembic/         迁移脚本（表结构变更一律走这里）
  postgres/init/   postgres 首启初始化 SQL（zhparser + zhcfg）
  tests/           pytest（真实 PG 测试库）
docker-compose.yml api / worker / postgres 三服务编排
```
