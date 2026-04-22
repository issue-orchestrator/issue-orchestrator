"""Tests for OrchestratorHttpApi request handling."""

from __future__ import annotations

import asyncio
import threading

import httpx

import pytest

from issue_orchestrator.execution.orchestrator_http_api import (
    OrchestratorHttpApi,
    OrchestratorAsyncHttpApi,
)
from tests.unit.threading_helpers import join_or_fail, wait_for_event, wait_for_async_event


class DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


def test_request_refreshes_base_url_on_request_error():
    calls: list[str] = []

    class FlakyClient:
        def __init__(self):
            self.fail_first = True

        def request(self, method, url, json=None, headers=None):
            calls.append(url)
            if self.fail_first:
                self.fail_first = False
                raise httpx.RequestError("boom", request=httpx.Request(method, url))
            return DummyResponse({"ok": True})

        def close(self) -> None:
            return None

    api = OrchestratorHttpApi(
        base_url_provider=lambda: "http://old",
        refresh_base_url=lambda: "http://new",
        client=FlakyClient(),
    )

    assert api.status() == {"ok": True}
    assert calls == ["http://old/api/status", "http://new/api/status"]


def test_client_requests_are_serialized():
    class ConcurrencyClient:
        def __init__(self):
            self.active = 0
            self.concurrent = 0
            self.lock = threading.Lock()
            self.start_event = threading.Event()
            self.release_event = threading.Event()

        def request(self, method, url, json=None, headers=None):
            with self.lock:
                self.active += 1
                if self.active > 1:
                    self.concurrent += 1
            self.start_event.set()
            wait_for_event(self.release_event, timeout=1.0, label="release_event")
            with self.lock:
                self.active -= 1
            return DummyResponse({"ok": True})

        def close(self) -> None:
            return None

    client = ConcurrencyClient()
    api = OrchestratorHttpApi(
        base_url_provider=lambda: "http://test",
        client=client,
    )

    def worker():
        api.status()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    wait_for_event(client.start_event, timeout=1.0, label="start_event")
    client.release_event.set()
    for index, thread in enumerate(threads, start=1):
        join_or_fail(thread, timeout=2, label=f"worker-{index}")

    assert client.concurrent == 0


@pytest.mark.asyncio
async def test_async_request_refreshes_base_url_on_request_error():
    calls: list[str] = []

    class FlakyAsyncClient:
        def __init__(self):
            self.fail_first = True

        async def request(self, method, url, json=None, headers=None):
            calls.append(url)
            if self.fail_first:
                self.fail_first = False
                raise httpx.RequestError("boom", request=httpx.Request(method, url))
            return DummyResponse({"ok": True})

        async def aclose(self) -> None:
            return None

    api = OrchestratorAsyncHttpApi(
        base_url_provider=lambda: "http://old",
        refresh_base_url=lambda: "http://new",
        client=FlakyAsyncClient(),
    )

    assert await api.status() == {"ok": True}
    assert calls == ["http://old/api/status", "http://new/api/status"]


@pytest.mark.asyncio
async def test_async_client_allows_concurrent_requests():
    class AsyncConcurrencyClient:
        def __init__(self):
            self.active = 0
            self.concurrent = 0
            self.lock = threading.Lock()
            self.start_event = asyncio.Event()
            self.release_event = asyncio.Event()

        async def request(self, method, url, json=None, headers=None):
            with self.lock:
                self.active += 1
                if self.active > 1:
                    self.concurrent += 1
            self.start_event.set()
            await wait_for_async_event(self.release_event, timeout=1.0, label="release_event")
            with self.lock:
                self.active -= 1
            return DummyResponse({"ok": True})

        async def aclose(self) -> None:
            return None

    client = AsyncConcurrencyClient()
    api = OrchestratorAsyncHttpApi(
        base_url_provider=lambda: "http://test",
        client=client,
    )

    task1 = asyncio.create_task(api.status())
    task2 = asyncio.create_task(api.status())
    await wait_for_async_event(client.start_event, timeout=1.0, label="start_event")
    client.release_event.set()
    await asyncio.gather(task1, task2)

    assert client.concurrent >= 1
