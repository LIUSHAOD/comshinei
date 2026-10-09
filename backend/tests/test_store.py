"""tests/test_store.py — Redis 存取层单测（fakeredis，无需真实 Redis）"""

import pytest

from app.runtime import store


class TestEvents:
    async def test_push_and_replay(self, fake_redis):
        e1 = await store.push_event("p1", "stage_change", {"stage": "lineart"})
        e2 = await store.push_event("p1", "candidates", {"candidates": []})
        assert e1["seq"] == 1 and e2["seq"] == 2

        events = await store.get_events("p1", after_seq=0)
        assert [e["type"] for e in events] == ["stage_change", "candidates"]

        # 断线重连：只回放 after_seq 之后的
        events = await store.get_events("p1", after_seq=1)
        assert [e["type"] for e in events] == ["candidates"]

    async def test_events_isolated_by_project(self, fake_redis):
        await store.push_event("p1", "stage_change", {})
        assert await store.get_events("p2") == []


class TestJob:
    async def test_set_and_get(self, fake_redis):
        await store.set_job_fields("p1", {"stage": "selecting", "candidates": [{"id": "a"}]})
        job = await store.get_job("p1")
        assert job["stage"] == "selecting"
        assert job["candidates"] == [{"id": "a"}]

    async def test_none_values_skipped(self, fake_redis):
        """None 不写入（防变成字符串 "None" 污染恢复现场）。"""
        await store.set_job_fields("p1", {"stage": "selecting", "lineart_url": None})
        job = await store.get_job("p1")
        assert job["stage"] == "selecting"
        assert "lineart_url" not in job

    async def test_empty_job(self, fake_redis):
        assert await store.get_job("nobody") == {}


class TestExcludes:
    async def test_add_and_get(self, fake_redis):
        await store.add_excludes("p1", ["a", "b"])
        await store.add_excludes("p1", ["b", "c"])  # set 语义去重
        assert await store.get_excludes("p1") == ["a", "b", "c"]

    async def test_add_empty_noop(self, fake_redis):
        await store.add_excludes("p1", [])
        assert await store.get_excludes("p1") == []
