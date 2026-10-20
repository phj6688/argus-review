"""Reviewer and judge requests share one process-wide budget."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from argus import client
from argus.jarvis import automark


@pytest.mark.parametrize("lane", ["reviewer", "judge", "router", "mixed"])
def test_ten_callers_never_exceed_three(lane, monkeypatch):
    monkeypatch.setenv("ARGUS_PROVIDER", "gateway")
    monkeypatch.setattr(client, "_llm_semaphore", threading.BoundedSemaphore(3))
    lock = threading.Lock()
    peak = 0
    active = 0
    holders = threading.Barrier(3)
    released = threading.Event()

    def observe():
        nonlocal peak, active
        with lock:
            active += 1
            peak = max(peak, active)
            assert client.llm_in_flight() <= 3
        if not released.is_set():
            holders.wait(timeout=3)
            released.set()
        with lock:
            active -= 1

    def create(**kwargs):
        observe()
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="result", input={})])

    def post(url, **kwargs):
        observe()
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"is_real": True, "confidence": 0.9, "reasoning": "checked"}
                            ),
                            "tool_calls": [{"function": {"name": "result", "arguments": "{}"}}],
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(
        client, "build_client", lambda *args: SimpleNamespace(messages=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.setattr(automark, "_judge_headers", lambda: {})
    monkeypatch.setattr(automark, "_assert_non_anthropic", lambda *a: None)
    monkeypatch.setattr(automark, "_judge_user", lambda _: "test")
    monkeypatch.setattr(automark, "_parse_verdict", json.loads)
    barrier = threading.Barrier(10)

    def call(index):
        barrier.wait(timeout=3)
        selected = ["judge", "router", "reviewer"][index % 3] if lane == "mixed" else lane
        if selected == "judge":
            return automark.default_judge(
                {}, url="https://test.invalid/complete", model="test-model"
            )
        return client.complete_tool(
            "test/model" if selected == "router" else "test-model",
            "",
            "",
            client.ToolSpec("result", {}, 10),
        )

    with ThreadPoolExecutor(10) as pool:
        list(pool.map(call, range(10)))
    assert peak == 3
    assert client.llm_in_flight() == 0


def test_slot_released_after_failure(monkeypatch):
    monkeypatch.setenv("ARGUS_PROVIDER", "gateway")
    def fail(**kwargs):
        raise RuntimeError("failed")

    monkeypatch.setattr(
        client, "build_client", lambda *args: SimpleNamespace(messages=SimpleNamespace(create=fail))
    )
    with pytest.raises(RuntimeError):
        client.complete_tool("test-model", "", "", client.ToolSpec("result", {}, 10))
    assert client.llm_in_flight() == 0
