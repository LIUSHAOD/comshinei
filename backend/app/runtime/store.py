"""app/runtime/store.py — 项目运行时状态与 SSE 事件的 Redis 存取层

Key 规划（计划 §6）：
- sse:{project_id}     list，进度事件缓冲（断线重连按 seq 回放），封顶 200 条，TTL 24h
- job:{project_id}     hash，任务运行时状态（stage、候选中间结果等），多 worker 共享
- exclude:{project_id} set，重新查询已排除的风格图 id，TTL 24h

事件信封：{"seq": int, "type": str, "data": dict, "ts": float}，seq 自增。
"""

import json
import time
from typing import Any

from app.db.redis import get_redis

EVENTS_TTL = 24 * 3600
EVENTS_MAX_LEN = 200


def _events_key(pid: str) -> str:
    return f"sse:{pid}"


def _seq_key(pid: str) -> str:
    return f"sse_seq:{pid}"


def _job_key(pid: str) -> str:
    return f"job:{pid}"


def _exclude_key(pid: str) -> str:
    return f"exclude:{pid}"


async def push_event(pid: str, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """追加事件到缓冲并返回完整信封。seq 用独立计数器原子自增，多发布者不打架。"""
    redis = await get_redis()
    seq = await redis.incr(_seq_key(pid))
    await redis.expire(_seq_key(pid), EVENTS_TTL)
    event = {"seq": seq, "type": event_type, "data": data, "ts": time.time()}
    key = _events_key(pid)
    await redis.rpush(key, json.dumps(event, ensure_ascii=False))
    await redis.ltrim(key, -EVENTS_MAX_LEN, -1)
    await redis.expire(key, EVENTS_TTL)
    return event


async def get_events(pid: str, after_seq: int = 0) -> list[dict[str, Any]]:
    """取 seq > after_seq 的全部事件（重连回放用；列表封顶 200 条，按 seq 过滤）。"""
    redis = await get_redis()
    raws = await redis.lrange(_events_key(pid), 0, -1)
    events = [json.loads(r) for r in raws]
    return [e for e in events if e["seq"] > after_seq]


async def set_job_fields(pid: str, fields: dict[str, Any]) -> None:
    """写 job hash；dict/list 值自动 JSON 序列化；None 值跳过（防写成字符串 "None"）。"""
    redis = await get_redis()
    mapping = {
        k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        for k, v in fields.items()
        if v is not None
    }
    if mapping:
        await redis.hset(_job_key(pid), mapping=mapping)
        await redis.expire(_job_key(pid), EVENTS_TTL)


async def get_job(pid: str) -> dict[str, Any]:
    """读 job hash；JSON 字段自动反序列化。"""
    redis = await get_redis()
    raw = await redis.hgetall(_job_key(pid))
    result: dict[str, Any] = {}
    for k, v in raw.items():
        try:
            result[k] = json.loads(v)
        except (json.JSONDecodeError, TypeError):
            result[k] = v
    return result


async def add_excludes(pid: str, ids: list[str]) -> None:
    if not ids:
        return
    redis = await get_redis()
    await redis.sadd(_exclude_key(pid), *ids)
    await redis.expire(_exclude_key(pid), EVENTS_TTL)


async def get_excludes(pid: str) -> list[str]:
    redis = await get_redis()
    return sorted(await redis.smembers(_exclude_key(pid)))
