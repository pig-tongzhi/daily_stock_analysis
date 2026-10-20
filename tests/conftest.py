# -*- coding: utf-8 -*-
"""Pytest compatibility hooks."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import shutil
import sqlite3
import tempfile
import time
import threading
from collections.abc import Awaitable, Callable
from contextvars import copy_context
from functools import wraps
from pathlib import Path
from typing import Any, Iterator, TypeVar
from warnings import warn

import anyio.to_thread
import fastapi.testclient
import httpx
import pytest
import starlette.testclient
from anyio._backends import _asyncio

T = TypeVar("T")

_original_call_soon_threadsafe = asyncio.BaseEventLoop.call_soon_threadsafe


async def _shutdown_default_executor_inline(
    self: asyncio.BaseEventLoop,
    timeout: float | None = None,
) -> None:
    """Avoid lost wakeups while asyncio.run() tears down test-only executors."""
    del timeout
    executor = getattr(self, "_default_executor", None)
    if executor is None:
        return
    self._executor_shutdown_called = True
    self._default_executor = None
    executor.shutdown(wait=True)


def _call_soon_threadsafe_with_extra_wakeup(
    self: asyncio.BaseEventLoop,
    callback,
    *args,
    context=None,
):
    """Wake the selector again for sandboxed test runs where the first wake is lost."""
    handle = _original_call_soon_threadsafe(self, callback, *args, context=context)
    write_to_self = getattr(self, "_write_to_self", None)
    if callable(write_to_self):
        write_to_self()
        threading.Timer(0.001, write_to_self).start()
    return handle


asyncio.BaseEventLoop.call_soon_threadsafe = _call_soon_threadsafe_with_extra_wakeup
asyncio.BaseEventLoop.shutdown_default_executor = _shutdown_default_executor_inline


async def _run_sync_via_asyncio_to_thread(
    func: Callable[..., T],
    *args: Any,
    abandon_on_cancel: bool = False,
    cancellable: bool | None = None,
    limiter: Any = None,
) -> T:
    """Use asyncio's executor path when AnyIO worker queues miss wakeups."""
    del abandon_on_cancel, limiter
    if cancellable is not None:
        warn(
            "The `cancellable=` keyword argument to `anyio.to_thread.run_sync` is "
            "deprecated since AnyIO 4.1.0; use `abandon_on_cancel=` instead",
            DeprecationWarning,
            stacklevel=2,
        )
    future: concurrent.futures.Future[T] = concurrent.futures.Future()
    context = copy_context()

    def runner() -> None:
        try:
            future.set_result(context.run(func, *args))
        except BaseException as exc:
            future.set_exception(exc)

    threading.Thread(target=runner, name="pytest-anyio-worker", daemon=True).start()
    while not future.done():
        await asyncio.sleep(0.001)
    return future.result()


def _wait_for_cross_thread_result(loop: asyncio.AbstractEventLoop, future: concurrent.futures.Future[T]) -> T:
    write_to_self = getattr(loop, "_write_to_self", None)
    while not future.done():
        if callable(write_to_self):
            write_to_self()
        time.sleep(0.001)
    return future.result()


def _run_sync_from_thread_with_wakeup(
    cls,
    func: Callable[..., T],
    args: tuple[Any, ...],
    token: object,
) -> T:
    @wraps(func)
    def wrapper() -> None:
        try:
            _asyncio.set_current_async_library("asyncio")
            future.set_result(func(*args))
        except BaseException as exc:
            future.set_exception(exc)
            if not isinstance(exc, Exception):
                raise

    loop = token or _asyncio.threadlocals.current_token.native_token
    if loop.is_closed():
        raise _asyncio.RunFinishedError
    future: concurrent.futures.Future[T] = concurrent.futures.Future()
    loop.call_soon_threadsafe(wrapper)
    return _wait_for_cross_thread_result(loop, future)


def _run_async_from_thread_with_wakeup(
    cls,
    func: Callable[..., Awaitable[T]],
    args: tuple[Any, ...],
    token: object,
) -> T:
    loop = token or _asyncio.threadlocals.current_token.native_token
    if loop.is_closed():
        raise _asyncio.RunFinishedError
    context = copy_context()
    context.run(_asyncio.set_current_async_library, "asyncio")
    future = context.run(asyncio.run_coroutine_threadsafe, func(*args), loop=loop)
    return _wait_for_cross_thread_result(loop, future)


anyio.to_thread.run_sync = _run_sync_via_asyncio_to_thread
_asyncio.AsyncIOBackend.run_sync_from_thread = classmethod(_run_sync_from_thread_with_wakeup)
_asyncio.AsyncIOBackend.run_async_from_thread = classmethod(_run_async_from_thread_with_wakeup)


class _ThreadlessTestClient:
    """Small TestClient replacement that avoids AnyIO's cross-thread portal."""

    def __init__(
        self,
        app,
        base_url: str = "http://testserver",
        raise_server_exceptions: bool = True,
        follow_redirects: bool = True,
        **_: Any,
    ) -> None:
        self.app = app
        self.base_url = base_url
        self.raise_server_exceptions = raise_server_exceptions
        self.follow_redirects = follow_redirects
        self.cookies = httpx.Cookies()
        self._lifespan_ctx = None
        self._lifespan_enter_count = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_client: httpx.AsyncClient | None = None

    def _get_lifespan_context(self):
        return getattr(getattr(self.app, "router", None), "lifespan_context", None)

    def _build_async_client(self, follow_redirects: bool) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(
            app=self.app,
            raise_app_exceptions=self.raise_server_exceptions,
        )
        return httpx.AsyncClient(
            transport=transport,
            base_url=self.base_url,
            follow_redirects=follow_redirects,
            cookies=self.cookies,
        )

    def __enter__(self):
        if self._lifespan_enter_count == 0:
            self._loop = asyncio.new_event_loop()
            lifespan_context = self._get_lifespan_context()
            if callable(lifespan_context):
                self._lifespan_ctx = lifespan_context(self.app)
                self._loop.run_until_complete(self._lifespan_ctx.__aenter__())
            self._async_client = self._build_async_client(self.follow_redirects)
        self._lifespan_enter_count += 1
        return self

    def __exit__(self, *args: Any) -> None:
        if self._lifespan_enter_count == 0:
            return None

        self._lifespan_enter_count -= 1
        if self._lifespan_enter_count == 0 and self._loop is not None:
            try:
                async def _close() -> None:
                    if self._async_client is not None:
                        await self._async_client.aclose()
                    if self._lifespan_ctx is not None:
                        await self._lifespan_ctx.__aexit__(*args)

                self._loop.run_until_complete(_close())
            finally:
                self._lifespan_ctx = None
                self._async_client = None
                self._loop.close()
                self._loop = None
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        follow_redirects = kwargs.pop("follow_redirects", self.follow_redirects)
        kwargs.pop("allow_redirects", None)

        if self._lifespan_enter_count > 0 and self._loop is not None and self._async_client is not None:
            response = self._loop.run_until_complete(
                self._async_client.request(method, url, follow_redirects=follow_redirects, **kwargs)
            )
            self.cookies = httpx.Cookies(self._async_client.cookies)
            return response

        async def _send() -> httpx.Response:
            async with self._build_async_client(follow_redirects) as client:
                response = await client.request(method, url, **kwargs)
                self.cookies = httpx.Cookies(client.cookies)
                return response

        return asyncio.run(_send())

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("HEAD", url, **kwargs)


fastapi.testclient.TestClient = _ThreadlessTestClient
starlette.testclient.TestClient = _ThreadlessTestClient


# ============================================================
# 测试数据库隔离：绝不写生产库
# ============================================================

def _production_database_path() -> Path:
    """Resolve the database the app would use without isolation."""
    configured = os.environ.get("DATABASE_PATH")
    if configured:
        return Path(configured).expanduser()
    try:
        from src.config import get_config

        value = getattr(get_config(), "database_path", None)
        if value:
            return Path(str(value)).expanduser()
    except Exception:
        pass
    return Path(__file__).resolve().parent.parent / "data" / "stock_analysis.db"


def _reset_database_singletons() -> None:
    try:
        from src.config import Config

        Config.reset_instance()
    except Exception:
        pass
    try:
        from src.storage import DatabaseManager

        DatabaseManager.reset_instance()
    except Exception:
        pass


# session fixture 把 DATABASE_PATH 指向临时副本；但既有测试里有大量这种写法：
#
#     os.environ["DATABASE_PATH"] = str(self.db_path)   # 指向自己的临时库
#     ...
#     os.environ.pop("DATABASE_PATH", None)             # 收尾时直接删掉
#
# pop 会把变量删干净，于是**后续测试**回退到默认的生产路径
# `./data/stock_analysis.db` —— session fixture 的保护就此失效。实测表现：
# 单独跑 writing 测试不污染，跑全量却往生产库写入 7 行夹具。
#
# 所以需要一个 function 级的守卫：每个测试开始前把 DATABASE_PATH 重新指回
# 会话副本。它不覆盖那些自己设了路径的测试（它们在测试体内赋值，晚于本守卫），
# 只保证"没人设置时"不会落到生产库。
_SESSION_DB_COPY: list = []


@pytest.fixture(autouse=True)
def _guard_production_database(monkeypatch, _isolate_production_database) -> Iterator[None]:
    """防止测试收尾 pop 掉 DATABASE_PATH 后落到生产库。"""
    if not _SESSION_DB_COPY:
        yield
        return
    copy_path = _SESSION_DB_COPY[0]
    if os.environ.get("DATABASE_PATH") != copy_path:
        monkeypatch.setenv("DATABASE_PATH", copy_path)
    yield


@pytest.fixture(scope="session", autouse=True)
def _isolate_production_database() -> Iterator[None]:
    """Point the whole test session at a throwaway copy of the database.

    为什么需要它：不少既有测试会走真实的持久化路径（选股快照会写 market_snapshot），
    它们只 patch 了数据源、没有隔离数据库，于是测试夹具会被写进
    ``data/stock_analysis.db`` —— 实测写入了 ``snapshot_source='sina'`` 的假行，
    并在最新日期上盖住了当天真实的 5565 行横截面，连问股本地工具都被带偏。

    做法：会话开始时用 sqlite 的 backup API 把生产库（含 WAL 中已提交的数据）
    复制成临时文件，再把 ``DATABASE_PATH`` 指过去。这样：

    * 需要真实数据的只读测试（个股档案等）仍然看到真实数据 —— 副本里有；
    * 任何写库的测试都只写副本，整轮测试后生产库逐字节不变。

    自己定义数据库的测试（各自 fixture 里设置 ``DATABASE_PATH``）不受影响。
    """
    source = _production_database_path()
    if not source.exists():
        yield
        return

    tmpdir = Path(tempfile.mkdtemp(prefix="dsa-pytest-db-"))
    destination = tmpdir / "stock_analysis.db"
    source_connection = sqlite3.connect(str(source))
    try:
        destination_connection = sqlite3.connect(str(destination))
        try:
            with destination_connection:
                source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
    finally:
        source_connection.close()

    previous = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(destination)
    _SESSION_DB_COPY.clear()
    _SESSION_DB_COPY.append(str(destination))
    _reset_database_singletons()
    try:
        yield
    finally:
        _SESSION_DB_COPY.clear()
        if previous is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = previous
        _reset_database_singletons()
        shutil.rmtree(tmpdir, ignore_errors=True)

