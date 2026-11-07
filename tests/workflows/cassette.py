"""Record real Claude and registry traffic once, then replay it offline.

Run with `TRIALVIZ_RECORD=1` to call the real services and rewrite the cassettes. Without
it, every request is answered from the cassette, and the replay is strict:

- A registry request that was never recorded fails the test, naming the request. If a
  code change alters any query the pipeline sends, the workflow notices.
- Each cassette stores a fingerprint of the Claude prompt and output schema. If either
  has changed since recording, replaying the old answer would test a reading the current
  prompt might not produce, so the test fails and asks for a re-record instead.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import httpx

from trialviz.query.interpreters.claude import OUTPUT_SCHEMA, SYSTEM_PROMPT, MessagesClient

RECORDING = os.environ.get("TRIALVIZ_RECORD") == "1"
CASSETTES = Path(__file__).parent / "cassettes"

PROMPT_FINGERPRINT = hashlib.sha256(
    (SYSTEM_PROMPT + json.dumps(OUTPUT_SCHEMA, sort_keys=True)).encode()
).hexdigest()[:16]


def request_key(request: httpx.Request) -> str:
    return f"{request.url.path}?{urlencode(sorted(request.url.params.multi_items()))}"


class Cassette:
    def __init__(self, name: str) -> None:
        self.path = CASSETTES / f"{name}.json"
        self.registry: dict[str, Any] = {}
        self.claude: list[dict[str, Any]] = []
        self.fingerprint = PROMPT_FINGERPRINT
        if not RECORDING:
            if not self.path.exists():
                raise FileNotFoundError(
                    f"{self.path.name} is missing. Record it with TRIALVIZ_RECORD=1."
                )
            data = json.loads(self.path.read_text())
            self.registry, self.claude, self.fingerprint = (
                data["registry"],
                data["claude"],
                data["fingerprint"],
            )
        self.sent: list[str] = []
        """Registry requests made during this run, in order."""

    def check_prompt_unchanged(self) -> None:
        if self.claude and self.fingerprint != PROMPT_FINGERPRINT:
            raise AssertionError(
                f"{self.path.name} was recorded with a different Claude prompt or schema. "
                "Re-record it with TRIALVIZ_RECORD=1."
            )

    def registry_transport(
        self, failures: Mapping[int, int] | None = None
    ) -> httpx.AsyncBaseTransport:
        """`failures` maps a request's position in the run (counting from 0, retries
        included) to a status code served instead of the recorded answer."""
        if RECORDING:
            return _RecordingTransport(self)
        return _ReplayTransport(self, dict(failures or {}))

    def claude_messages(self, real: MessagesClient | None = None) -> MessagesClient:
        if RECORDING:
            assert real is not None, "recording needs a real Anthropic client"
            return _RecordingMessages(self, real)
        self.check_prompt_unchanged()
        return _ReplayMessages(self)

    def save(self) -> None:
        if not RECORDING:
            return
        CASSETTES.mkdir(exist_ok=True)
        payload = {
            "fingerprint": PROMPT_FINGERPRINT,
            "claude": self.claude,
            "registry": self.registry,
        }
        self.path.write_text(
            json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
        )


class _ReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, cassette: Cassette, failures: dict[int, int]) -> None:
        self._cassette = cassette
        self._failures = failures

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = request_key(request)
        position = len(self._cassette.sent)
        self._cassette.sent.append(key)
        if position in self._failures:
            return httpx.Response(self._failures[position], text="injected failure")
        if key not in self._cassette.registry:
            raise AssertionError(f"unrecorded registry request: {key}")
        return httpx.Response(200, json=self._cassette.registry[key])


class _RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, cassette: Cassette) -> None:
        self._cassette = cassette
        self._real = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._real.handle_async_request(request)
        body = await response.aread()
        key = request_key(request)
        self._cassette.sent.append(key)
        if response.status_code == 200:
            self._cassette.registry[key] = json.loads(body)
        # The body is already decompressed, so its encoding headers no longer describe it.
        headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in {"content-encoding", "content-length"}
        }
        return httpx.Response(response.status_code, content=body, headers=headers)


class _ReplayMessages:
    def __init__(self, cassette: Cassette) -> None:
        self._replies = list(cassette.claude)

    async def create(self, **_: Any) -> Any:
        if not self._replies:
            raise AssertionError("Claude was called more times than were recorded")
        reply = self._replies.pop(0)
        return SimpleNamespace(
            stop_reason=reply["stop_reason"],
            content=[SimpleNamespace(type="text", text=reply["text"])],
            usage=SimpleNamespace(**reply["usage"]),
        )


class _RecordingMessages:
    def __init__(self, cassette: Cassette, real: MessagesClient) -> None:
        self._cassette = cassette
        self._real = real

    async def create(self, **kwargs: Any) -> Any:
        response = await self._real.create(**kwargs)
        usage = response.usage
        self._cassette.claude.append(
            {
                "question": kwargs["messages"][0]["content"],
                "stop_reason": response.stop_reason,
                "text": next(b.text for b in response.content if b.type == "text"),
                "usage": {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_creation_input_tokens": usage.cache_creation_input_tokens or 0,
                    "cache_read_input_tokens": usage.cache_read_input_tokens or 0,
                },
            }
        )
        return response
