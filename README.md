# Comshinei — 室内设计 Agent

技术栈：Vue3 + FastAPI + LangGraph + MySQL + Redis + Qdrant + ComfyUI（裸机）。详见上级目录《开发计划.md》。

## 当前进度

- **M2 后端骨架**：uv 工程、config、db/redis 连接、models + alembic 迁移（含 `comfy_workflows` 表）、storage 路径管理、移植自 Muse-Studio 的 `ComfyUIRunner`（HTTP 轮询 + 媒体预上传）与 `workflow_parse`（(Input)/(Output) 后缀解析 + patch_workflow 参数注入）、工作流模板注册接口。
- **M3 LangGraph 阶段 1**：DesignState、`parse → [lineart ∥ retrieve] → gate` 图、Queue+emit 进度传送带（事件落 Redis，SSE 端点纯读 Redis，支持断线重连回放）、CLIP+Qdrant 检索（must_not exclude）、`POST /projects`（multipart 上传实拍图+需求）、`GET /projects/{id}`（刷新恢复现场）、`GET /projects/{id}/stream`（SSE）、`POST /projects/{id}/requery`（重查）、`GET /api/images/{ref}`（图片访问）。
- **M4 风格库管理**：`POST /api/styles`（批量上传，入库即向量化：落盘 → CLIP 批量编码 → Qdrant 批量 upsert → MySQL 元数据落行，逐文件容错）、`GET /api/styles`（分页列表）、`DELETE /api/styles/{id}`（同步删 Qdrant 点 + 文件 + 行）。验收：50 张图上传后检索 top-1 命中正确色系。
- **M5 LangGraph 阶段 2**：`prompt → generate → assemble` 子图、prompt_node（LLM 中文需求→英文 SD 提示词，Langfuse 可选观测）、generate_node（BrushNet 工作流注入 + 全局锁）、`POST /projects/{id}/confirm`（状态恢复续跑）、`POST /projects/{id}/cancel`（ComfyUI interrupt + 阶段回退）。事件流按阶段/按次自包含（confirm/requery 清空旧事件缓冲）；error 事件分 fatal/非 fatal，SSE 不被节点级非致命错误打断。
- **M6 前端**：Vue3 + TS + Pinia + vue-router + axios，CreateView / DesignView / GalleryView 三页、useSSE 断线重连（终态自动 close）、按 stage 恢复现场、重新查询/确认生成/取消/批量风格库管理。`cd frontend && npm install && npm run dev`（代理 /api→8000）。
- **M7 清理 + 部署**：cleanup_service（TTL 超龄项目连锅端 + Qdrant 孤儿点对账 + cleanup_rules 记录）、APScheduler 每 24h 随 backend 启动、`POST /api/cleanup/run` 手动触发、`DELETE /projects/{id}`；deploy/ 下 docker-compose + backend/frontend Dockerfile + nginx（/api 反代、SSE 关缓冲、SPA 回退）。

## 部署（docker compose）

```bash
cd deploy
docker compose up -d --build     # 一键起全栈（mysql/redis/qdrant/backend/frontend）
# 前端 http://localhost  后端 http://localhost:8000/docs
# ComfyUI 裸机不在 compose 内，backend 经 host.docker.internal:8188 连接
# LLM key 等敏感项读 ../backend/.env（gitignore，唯一来源）；基础设施地址由 compose environment 覆盖
```

镜像构建要点：Linux 用 CPU 版 torch（pyproject `[[tool.uv.index]] pytorch-cpu` + `[tool.uv.sources]`，
免数 GB CUDA 依赖）；pip/uv/npm 均走国内镜像加速。

## 全链验收事件序列

```
POST /projects     → stage_change(lineart) → progress → lineart_done → candidates → stage_change(selecting) → completed
POST confirm       → stage_change(prompting) → progress(LLM 提示词) → stage_change(generating) → progress → image_done → stage_change(done) → completed
POST requery       → candidates(新一批) → completed
```

> **中文检索限制（计划 §11 风险）**：首版 clip-ViT-B-32 是英文模型，中文需求文本直接检索
> 近邻质量差（实测中文查询不排序，英文 top-1 命中且 margin 清晰）。预留换 Chinese-CLIP
> （权重已缓存于本机 HF cache）：改编码模型与 collection 即可，`ClipService` 是统一出入口。
> 临时对策：M5 的 prompt_node 可先把中文需求翻成英文检索词。

## M3 端到端流程

```bash
# 0. 依赖服务：ComfyUI（裸机）+ Redis/Qdrant（Docker）
docker start comshinei-redis comshinei-qdrant

# 1. 风格图种子入库（真实风格图目录；测试可用 scripts 生成的样例图）
cd backend && uv run python ../scripts/import_styles.py <风格图目录>

# 2. 启动后端后创建项目（multipart）
curl -X POST http://127.0.0.1:8000/api/projects \
  -F "photo=@D:/photos/room.jpg" -F "requirements=北欧风客厅，暖色调"

# 3. 监听 SSE 进度（断线重连带 Last-Event-ID 自动续传）
curl -N http://127.0.0.1:8000/api/projects/<id>/stream
# 事件序列：stage_change(lineart) → lineart_done ∥ candidates → stage_change(selecting) → completed

# 4. 重查（exclude 累加，新候选不含已看）
curl -X POST http://127.0.0.1:8000/api/projects/<id>/requery

# 5. 刷新恢复现场
curl http://127.0.0.1:8000/api/projects/<id>
```

## 快速开始（backend）

```bash
cd backend
cp ../.env.example .env        # 默认 COMFYUI_HOST=127.0.0.1:8188，一般不用改
uv sync                        # 安装依赖（uv 管理；已执行过则秒过）
```

数据库二选一：

```bash
# A. 快速试（零依赖，sqlite 兜底，仅建议本地联调用）
#    Windows Git Bash / Linux:
DATABASE_URL="sqlite:///./data/dev.db" uv run uvicorn app.main:app --reload --port 8000

# B. 正式（MySQL，与部署一致）
#    先起 MySQL 8 并建好 comshinei 库（utf8mb4），.env 里配 DATABASE_URL，然后：
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
```

> 建表双轨说明：启动时会 `create_all` 兜底建表（DB 未起不阻断）；若库从未被 alembic
> 管理（无 alembic_version 记录），建表后自动盖章到当前最新迁移，之后跑
> `alembic upgrade head` 为幂等空操作，两条路径不冲突。

启动后：

- Swagger 交互式接口文档（无前端时的主要测试入口）：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health>

## 无前端测试接口的三条路

1. **浏览器 Swagger UI**：打开 `/docs`，依次试 `GET /api/comfy/ping` →
   `POST /api/workflows`（json 字段粘贴导出的 API JSON）→ `POST /api/comfy/run`。

2. **注册脚本 + curl**：

   ```bash
   # 把 M1 导出的两条工作流注册进 comfy_workflows 表（打印解析出的 inputs/outputs）
   python scripts/register_workflows.py

   # 直调 ComfyUI 跑 lineart（图片输入填本地绝对路径，自动预上传）
   curl -X POST http://127.0.0.1:8000/api/comfy/run \
     -H "Content-Type: application/json" \
     -d '{"workflow_key": "lineart", "input_values": {"实拍图": "D:/photos/room.jpg"}}'
   # → data.outputs: {"线稿": "...png", "结构线": ..., "深度图": ..., "分割图": ...}
   #   产物落盘 backend/data/outputs/debug/<时间戳>/
   ```

3. **pytest 集成测试**：`cd backend && uv run pytest -m integration -v`
   （用 tests/fixtures/ 里的真实模板跑通 lineart→generate 全链）
   单测（无需外部服务）：`uv run pytest`

## 目录

```
backend/
├── app/
│   ├── main.py            # FastAPI 入口（CORS / 中间件 / 异常处理 / 路由）
│   ├── config.py          # pydantic-settings 读取 .env
│   ├── api/v1/            # workflows 模板注册 / comfy 直调（M2）；projects/search/stream 待 M3-M5
│   ├── services/          # comfy.py（ComfyUIRunner）/ workflow_parse.py
│   ├── models/            # SQLAlchemy ORM（projects/style_images/comfy_workflows/cleanup_rules）
│   ├── repositories/      # 数据访问层
│   ├── db/                # MySQL engine / Redis 连接
│   ├── storage/paths.py   # uploads / outputs 统一路径管理
│   ├── middleware/        # 全局异常、请求日志
│   └── utils/
├── alembic/               # 数据库迁移
└── tests/                 # 单测 + 真 ComfyUI 集成测试
scripts/
└── register_workflows.py  # 注册 M1 导出的工作流模板到后端
```
