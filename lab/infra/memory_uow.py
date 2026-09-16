#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""内存版 UnitOfWork / 仓储：让 SUT 的 flow 摆脱 PostgreSQL + Redis。

为什么必须做这个：
- SUT 的 PlannerReActFlow 第一件事就是 `uow.session.get_by_id(session_id)`，
  拿不到会话直接抛错，根本进不去主循环；执行过程中还要读写 Agent 记忆、更新会话状态。
  不提供 UoW 就完全无法 headless 调用。
- 评测要跑量（Step 4 是"20 个任务 × n 次重复"），起 PG + Redis 是纯负担，
  而且会让单任务耗时里混进数据库抖动 —— 我们要测的是 Agent，不是数据库。
- 副产品：内存实现让**任务之间天然隔离**，前一个任务的状态不会污染后一个任务的评测结果。

实现要点（读代码时重点看这两点）：
1. `_Store` 被所有 UoW 实例**共享**。SUT 里 `uow_factory()` 会被调用多次
   （flow 一次、每个 Agent 各一次），如果每次 new 一个 Store，数据就丢了。
2. `get_by_id` 直接返回同一个 Session 对象引用 → flow 里的原地修改自动生效，
   等价于"隐式自动提交"。这与内存版的定位一致，也省掉了伪造事务的复杂度。
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Dict, List, Optional

from lab.bootstrap import ensure_sut_on_path

ensure_sut_on_path()

from app.domain.models.event import BaseEvent  # noqa: E402
from app.domain.models.file import File  # noqa: E402
from app.domain.models.memory import Memory  # noqa: E402
from app.domain.models.session import Session, SessionStatus  # noqa: E402
from app.domain.repositories.uow import IUnitOfWork  # noqa: E402


class InMemoryStore:
    """进程内共享存储：会话表 + 文件表。"""

    def __init__(self) -> None:
        self.sessions: Dict[str, Session] = {}
        self.files: Dict[str, File] = {}


class InMemorySessionRepository:
    """SessionRepository 协议的内存实现。

    方法签名与 app/domain/repositories/session_repository.py 完全一致：
    这里是"照抄接口、替换实现"，所以调用方（SUT）不需要任何改动。
    """

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def save(self, session: Session) -> None:
        self._store.sessions[session.id] = session

    async def get_all(self) -> List[Session]:
        return list(self._store.sessions.values())

    async def get_by_id(self, session_id: str) -> Optional[Session]:
        return self._store.sessions.get(session_id)

    async def delete_by_id(self, session_id: str) -> None:
        self._store.sessions.pop(session_id, None)

    async def update_title(self, session_id: str, title: str) -> None:
        session = self._store.sessions.get(session_id)
        if session:
            session.title = title
            session.updated_at = datetime.now()

    async def update_latest_message(self, session_id: str, message: str, timestamp: datetime) -> None:
        session = self._store.sessions.get(session_id)
        if session:
            session.latest_message = message
            session.latest_message_at = timestamp
            session.updated_at = datetime.now()

    async def update_unread_message_count(self, session_id: str, count: int) -> None:
        session = self._store.sessions.get(session_id)
        if session:
            session.unread_message_count = count

    async def increment_unread_message_count(self, session_id: str) -> None:
        session = self._store.sessions.get(session_id)
        if session:
            session.unread_message_count += 1

    async def decrement_unread_message_count(self, session_id: str) -> None:
        session = self._store.sessions.get(session_id)
        if session:
            session.unread_message_count = max(0, session.unread_message_count - 1)

    async def update_status(self, session_id: str, status: SessionStatus) -> None:
        session = self._store.sessions.get(session_id)
        if session:
            session.status = status
            session.updated_at = datetime.now()

    async def add_event(self, session_id: str, event: BaseEvent) -> None:
        session = self._store.sessions.get(session_id)
        if session:
            session.events.append(event)

    async def add_file(self, session_id: str, file: File) -> None:
        session = self._store.sessions.get(session_id)
        if session:
            # 同名/同路径文件只保留一份，避免重复上传造成计数虚高
            session.files = [f for f in session.files if f.filepath != file.filepath]
            session.files.append(file)

    async def remove_file(self, session_id: str, file_id: str) -> None:
        session = self._store.sessions.get(session_id)
        if session:
            session.files = [f for f in session.files if f.id != file_id]

    async def get_file_by_path(self, session_id: str, filepath: str) -> Optional[File]:
        session = self._store.sessions.get(session_id)
        if not session:
            return None
        return next((f for f in session.files if f.filepath == filepath), None)

    async def save_memory(self, session_id: str, agent_name: str, memory: Memory) -> None:
        session = self._store.sessions.get(session_id)
        if session:
            session.memories[agent_name] = memory

    async def get_memory(self, session_id: str, agent_name: str) -> Memory:
        """取记忆；不存在则新建空记忆（与 DB 实现语义一致）。"""
        session = self._store.sessions.get(session_id)
        if session is None:
            return Memory()
        if agent_name not in session.memories:
            session.memories[agent_name] = Memory()
        return session.memories[agent_name]


class InMemoryFileRepository:
    """FileRepository 协议的内存实现（Step 1 用不到上传下载，只需满足接口）。"""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def save(self, file: File) -> None:
        self._store.files[file.id] = file

    async def get_by_id(self, file_id: str) -> Optional[File]:
        return self._store.files.get(file_id)


class InMemoryUnitOfWork(IUnitOfWork):
    """内存 UoW：满足 IUnitOfWork 抽象方法，但事务语义是"无操作"。

    为什么要如实实现 commit/rollback 而不是抛 NotImplementedError：
    SUT 的调用写法是 `async with self._uow:`，退出时会走 __aexit__。
    如果这里抛异常，正常路径也会被打断。内存版本没有"半提交"状态，
    commit/rollback 都是空操作，这是**语义上正确**的，不是偷懒。
    """

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store
        self.session = InMemorySessionRepository(store)
        self.file = InMemoryFileRepository(store)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def __aenter__(self) -> "InMemoryUnitOfWork":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        return None


def create_uow_factory() -> Callable[[], IUnitOfWork]:
    """创建共享同一份内存存储的 uow 工厂。

    用法：`uow_factory = create_uow_factory()`，然后把它传给 PlannerReActFlow。
    多次调用 `uow_factory()` 得到的是不同 UoW 实例，但看到的是同一份数据 ——
    这正是 SUT 期望的行为。
    """
    store = InMemoryStore()

    def _factory() -> IUnitOfWork:
        return InMemoryUnitOfWork(store)

    return _factory
