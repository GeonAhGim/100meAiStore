"""Minimal OpenAI-compatible local LLM client.

This is deliberately separate from AIOS and has no Codex integration. Callers
must pass an already-approved prompt; this module only talks to a local HTTP
endpoint and never claims a slot itself.
"""

from __future__ import annotations

import json
from urllib.request import Request, urlopen


def complete(prompt: str, *, endpoint: str = "http://127.0.0.1:8081", model: str = "local", timeout: int = 120) -> str:
    payload = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False}
    ).encode("utf-8")
    request = Request(
        endpoint.rstrip("/") + "/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - endpoint is localhost by contract
        data = json.loads(response.read().decode("utf-8"))
    return str(data["choices"][0]["message"]["content"])
