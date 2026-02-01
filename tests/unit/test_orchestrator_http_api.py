"""Tests for OrchestratorHttpApi request handling."""

from __future__ import annotations

import threading
import time

import httpx

from issue_orchestrator.execution.orchestrator_http_api import OrchestratorHttpApi


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

        def request(self, method, url, json=None):
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

        def request(self, method, url, json=None):
            with self.lock:
                self.active += 1
                if self.active > 1:
                    self.concurrent += 1
            time.sleep(0.05)
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

    barrier = threading.Barrier(2)

    def worker():
        barrier.wait(timeout=1)
        api.status()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert client.concurrent == 0
