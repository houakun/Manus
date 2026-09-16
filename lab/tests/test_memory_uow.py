#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""内存 UoW 的回归测试。

这是"lab 能脱离 PostgreSQL + Redis 运行"的验收证据。
最关键的用例是 test_multiple_uow_instances_share_the_same_store：
SUT 会多次调用 uow_factory()（flow 一次、每个 Agent 各一次），
如果每次 new 一份存储，Agent 记忆和会话状态就会静默丢失 ——
表现为"Agent 每轮都失忆"，很难排查。
"""

from __future__ import annotations

import pytest

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from app.domain.models.memory import Memory  # noqa: E402
from app.domain.models.session import Session, SessionStatus  # noqa: E402
from lab.infra.memory_uow import create_uow_factory  # noqa: E402


@pytest.mark.asyncio
async def test_multiple_uow_instances_share_the_same_store():
    """多个 UoW 实例必须看到同一份数据（SUT 的 uow_factory 会被调用多次）。"""
    factory = create_uow_factory()
    uow_a, uow_b = factory(), factory()

    async with uow_a:
        await uow_a.session.save(Session(id="s1"))

    async with uow_b:
        assert await uow_b.session.get_by_id("s1") is not None
        await uow_b.session.update_status("s1", SessionStatus.RUNNING)

    async with uow_a:
        session = await uow_a.session.get_by_id("s1")
        assert session.status == SessionStatus.RUNNING


@pytest.mark.asyncio
async def test_different_factories_are_isolated():
    """不同任务之间必须完全隔离，避免评测结果互相污染。"""
    factory_1, factory_2 = create_uow_factory(), create_uow_factory()

    async with factory_1() as uow:
        await uow.session.save(Session(id="only-in-1"))

    async with factory_2() as uow:
        assert await uow.session.get_by_id("only-in-1") is None


@pytest.mark.asyncio
async def test_memory_roundtrip_and_implicit_commit():
    """记忆读写 + "原地修改即生效"的隐式提交语义。"""
    factory = create_uow_factory()

    async with factory() as uow:
        await uow.session.save(Session(id="s2"))
        memory = await uow.session.get_memory("s2", "planner")
        assert memory.get_messages() == []
        memory.add_message({"role": "system", "content": "you are a planner"})
        await uow.session.save_memory("s2", "planner", memory)

    async with factory() as uow:
        memory = await uow.session.get_memory("s2", "planner")
        assert len(memory.get_messages()) == 1

    # 原地修改（不显式 save）也要生效：与内存实现的定位一致
    async with factory() as uow:
        memory = await uow.session.get_memory("s2", "planner")
        memory.add_message({"role": "user", "content": "hi"})
    async with factory() as uow:
        assert len((await uow.session.get_memory("s2", "planner")).get_messages()) == 2


@pytest.mark.asyncio
async def test_get_memory_returns_fresh_memory_when_absent():
    """取不存在的记忆要返回空记忆而不是 None（与 DB 实现语义一致，避免调用方判空遗漏）。"""
    async with create_uow_factory()() as uow:
        await uow.session.save(Session(id="s3"))
        memory = await uow.session.get_memory("s3", "never-used")
        assert isinstance(memory, Memory)
        assert memory.empty


@pytest.mark.asyncio
async def test_events_and_files_are_recorded():
    """事件与文件要能被记录和查询（flow 依赖事件来恢复最新 Plan）。"""
    from app.domain.models.event import TitleEvent
    from app.domain.models.file import File

    async with create_uow_factory()() as uow:
        await uow.session.save(Session(id="s4"))
        await uow.session.add_event("s4", TitleEvent(title="t"))
        await uow.session.add_file("s4", File(filepath="/home/ubuntu/a.txt"))

        session = await uow.session.get_by_id("s4")
        assert session.title != "" or session.events  # 事件已落库
        assert len(session.events) == 1
        assert (await uow.session.get_file_by_path("s4", "/home/ubuntu/a.txt")) is not None

        # 同路径重复添加只保留一份
        await uow.session.add_file("s4", File(filepath="/home/ubuntu/a.txt"))
        assert len((await uow.session.get_by_id("s4")).files) == 1
