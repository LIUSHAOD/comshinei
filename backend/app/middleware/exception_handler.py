"""app/middleware/exception_handler.py — 全局异常统一转 JSON 响应"""

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.utils.logger import logger
from app.utils.response import fail


async def http_exception_handler(_request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=fail(detail, code=exc.status_code),
    )


async def validation_exception_handler(_request: Request, exc: RequestValidationError):
    # 请求参数校验失败（422）同样走统一信封，与全局响应格式一致
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(x) for x in first.get("loc", []))
    detail = f"{loc}: {first.get('msg', '')}".strip(": ") or "请求参数校验失败"
    return JSONResponse(status_code=422, content=fail(detail, code=422))


async def value_error_handler(_request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content=fail(str(exc), code=400))


async def global_exception_handler(_request: Request, exc: Exception):
    logger.error(f"[UnhandledException] {type(exc).__name__}: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content=fail("服务器内部错误，请稍后重试", code=500))
