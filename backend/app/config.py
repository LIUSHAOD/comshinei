"""app/config.py — 应用配置中心（pydantic-settings 读取 .env）"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_BACKEND_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    DATABASE_URL: str = "mysql+pymysql://root:root@127.0.0.1:3306/comshinei?charset=utf8mb4"
    REDIS_URL: str = "redis://127.0.0.1:6379/0"

    QDRANT_URL: str = "http://127.0.0.1:6333"
    QDRANT_COLLECTION: str = "interior_styles"

    # CLIP 向量检索（M4 写入侧复用同一模型）
    CLIP_MODEL_NAME: str = "sentence-transformers/clip-ViT-B-32"
    CLIP_VECTOR_DIM: int = 512
    SEARCH_CANDIDATE_LIMIT: int = 8
    # huggingface.co 不可达时走镜像（如 https://hf-mirror.com）
    HF_ENDPOINT: str = ""

    # ComfyUI 裸机运行，backend 经 HTTP 连接
    COMFYUI_HOST: str = "127.0.0.1:8188"
    COMFYUI_TIMEOUT: int = 300

    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "deepseek-chat"

    LANGFUSE_HOST: str = ""
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""

    UPLOAD_DIR: str = "./data/uploads"
    OUTPUT_DIR: str = "./data/outputs"
    IMAGE_TTL_DAYS: int = 30
    CLEANUP_INTERVAL_HOURS: int = 24

    @property
    def comfy_base_url(self) -> str:
        """COMFYUI_HOST 允许省略 scheme（如 host.docker.internal:8188），统一补 http://。"""
        host = self.COMFYUI_HOST.rstrip("/")
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        return host

    def resolve_dir(self, path_str: str) -> Path:
        """相对路径一律相对 backend/ 根解析，与启动时 CWD 无关。"""
        p = Path(path_str)
        return p if p.is_absolute() else _BACKEND_ROOT / p


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
