# Comshinei — 室内设计 Agent

上传一张实拍房照片 + 一句中文装修需求，自动完成 **结构理解（线稿/深度/结构线）→ 风格图检索推荐 → 选图确认 → LLM 生成提示词 → ComfyUI 条件生图 → 效果图**，全程 SSE 实时推送进度。

技术栈：Vue3 + FastAPI + LangGraph + MySQL + Redis + Qdrant + ComfyUI（裸机）。

详细架构与流程见 [介绍.md](介绍.md)，测试说明见 [测试.md](测试.md)，开发计划见上级目录《开发计划.md》。

## 功能一览

- **项目主流程**：`POST /projects` 创建（multipart 上传）→ 阶段 1 并行图 `parse → [lineart ∥ retrieve] → gate` → 用户选风格 → `confirm` 续跑阶段 2 `prompt → generate → assemble` → 效果图
- **SSE 进度流**：事件真相存 Redis（seq 单调、封顶 200、TTL 24h），断线带 `Last-Event-ID` 自动续传；按阶段/按次自包含，error 分 fatal/非 fatal
- **风格检索**：CLIP（clip-ViT-B-32）+ Qdrant，`requery` 把已看候选累加进 exclude 集合换一批
- **风格库管理**：批量上传即向量化（落盘 → 批量编码 → Qdrant → MySQL，逐文件容错）、分页、删除
- **工作流即数据**：ComfyUI 模板存库，按 `(Input)/(Output)` 标题后缀解析动态参数，新增工作流零代码
- **任务控制**：取消（ComfyUI `/interrupt` + 阶段回退）、删除（连锅端）、TTL 定时清理 + Qdrant 孤儿对账（APScheduler）
- **前端三页**：CreateView（上传需求）/ DesignView（进度+选图+结果）/ GalleryView（风格库）

## 部署（docker compose 一键全栈）

```bash
cd deploy
docker compose up -d --build
# 前端 http://localhost   后端 API 文档 http://localhost:8000/docs
# ComfyUI 裸机不在 compose 内，backend 经 host.docker.internal:8188 连接
# LLM key 等敏感项读 ../backend/.env（gitignore，唯一来源）；基础设施地址由 compose environment 覆盖
```

镜像构建要点：Linux 用 CPU 版 torch（pyproject `[[tool.uv.index]] pytorch-cpu` + `[tool.uv.sources]`，免数 GB CUDA 依赖）；pip/uv/npm 均走国内镜像加速。

## 本地开发

```bash
# 后端
cd backend
cp ../.env.example .env        # 按需填 LLM_API_KEY；COMFYUI_HOST 默认 127.0.0.1:8188
uv sync

# 数据库二选一：
# A. sqlite 兜底（零依赖，仅本地联调）：
DATABASE_URL="sqlite:///./data/dev.db" uv run uvicorn app.main:app --reload --port 8000
# B. MySQL 正式（先建 utf8mb4 的 comshinei 库、配好 .env）：
uv run alembic upgrade head && uv run uvicorn app.main:app --reload --port 8000

# 前端（另开终端）
cd frontend && npm install && npm run dev   # http://localhost:5173，/api 代理到 8000
```

> 建表双轨说明：启动时 `create_all` 兜底建表（DB 未起不阻断）；若库从未被 alembic
> 管理，建表后自动盖章到当前最新迁移，之后 `alembic upgrade head` 为幂等空操作。

启动后入口：

- Swagger 接口文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health>

## 联调 / 验收

```bash
# 1. 依赖服务：ComfyUI（裸机）+ Redis/Qdrant（Docker）
docker start comshinei-redis comshinei-qdrant

# 2. 注册工作流模板 + 风格图种子入库
python scripts/register_workflows.py
cd backend && uv run python ../scripts/import_styles.py <风格图目录>

# 3. 跑测试：72 单测（无外部依赖）+ 5 集成测试（真服务，不可达自动 skip）
uv run pytest                      # 单测
uv run pytest -m integration -v    # 集成
```

全链验收事件序列：

```
POST /projects → stage_change(lineart) → progress → lineart_done ∥ candidates → stage_change(selecting) → completed
POST confirm   → stage_change(prompting) → progress(LLM) → stage_change(generating) → progress → image_done → stage_change(done) → completed
POST requery   → candidates(新一批) → completed
```

手动验收的 curl 全流程见 [测试.md](测试.md#六手动端到端验收无前端-curl-版)。

## 目录

```
comshinei/
├── README.md / 介绍.md / 测试.md
├── deploy/                      # docker compose 全栈 + nginx（SSE 关缓冲、SPA 回退）
├── scripts/                     # register_workflows.py / import_styles.py
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口（建表/盖章/Redis/APScheduler）
│   │   ├── config.py            # pydantic-settings
│   │   ├── api/v1/              # projects/search/stream/styles/workflows/comfy/images/cleanup
│   │   ├── runtime/             # LangGraph：state/graph/nodes/runner/store
│   │   ├── services/            # comfy / workflow_parse / clip / prompt / cleanup
│   │   ├── models/ repositories/ db/ storage/ middleware/ utils/
│   ├── alembic/                 # 数据库迁移
│   └── tests/                   # 72 单测 + 5 集成测试
└── frontend/                    # Vue3 SPA（CreateView / DesignView / GalleryView）
```

## 已知限制

- **中文检索**：clip-ViT-B-32 是英文模型，中文需求直接检索近邻质量差（实测中文不排序、英文 top-1 命中且 margin 清晰）。预留换 Chinese-CLIP：`ClipService` 是统一出入口，改编码模型与 collection 即可
- **生图串行**：8G 显存约束，生图节点全局锁 + ComfyUI 队列串行，并发项目排队
- **风格注入**：选中风格图目前只影响检索与展示，生图风格主要靠提示词；IPAdapter 注入为预留项
