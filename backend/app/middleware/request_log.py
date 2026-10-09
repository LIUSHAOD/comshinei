"""app/middleware/request_log.py"""

import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.utils.logger import logger


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        elapsed = int((time.time() - start) * 1000)
        logger.info(
            "%s %s %s %dms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
        )
        return response
