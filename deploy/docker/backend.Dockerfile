# backend 镜像：uv 管理依赖（uv sync --frozen）
# 基础镜像走 docker hub（ghcr 国内不稳定）；pip/uv 均用清华镜像加速
FROM python:3.13-slim

ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1

RUN pip install --no-cache-dir uv

WORKDIR /app

# 先锁文件层：依赖不变则命中构建缓存
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev

COPY backend/ .

EXPOSE 8000

# 启动先跑迁移再起服务（迁移幂等；与 create_all+stamp 双轨不冲突）
CMD ["sh", "-c", "uv run --no-sync alembic upgrade head && uv run --no-sync uvicorn app.main:app --host 0.0.0.0 --port 8000"]
