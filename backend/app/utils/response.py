"""app/utils/response.py — 统一响应信封 {code, message, data}"""

from typing import Any


def success(data: Any = None, message: str = "ok", code: int = 200) -> dict[str, Any]:
    return {"code": code, "message": message, "data": data}


def fail(message: str, code: int = 400, data: Any = None) -> dict[str, Any]:
    return {"code": code, "message": message, "data": data}
