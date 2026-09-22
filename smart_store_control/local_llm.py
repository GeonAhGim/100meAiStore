"""Minimal OpenAI-compatible local LLM client.

This is deliberately separate from AIOS and has no Codex integration. Callers
must pass an already-approved prompt; this module only talks to a local HTTP
endpoint and never claims a slot itself.
"""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection, HTTPException
from urllib.parse import urlsplit


def complete(prompt: str, *, endpoint: str = "http://127.0.0.1:8081", model: str = "local", timeout: int = 900,
             max_tokens: int = 6000) -> str:
    """Chat completion against the local llama.cpp server with a hard deadline.

    ``timeout`` is a total deadline for the whole call, not a per-recv socket
    timeout. A per-recv timeout let a request queued behind AIOS sit in
    ``_read_status`` for ten minutes with ``timeout=30`` in effect, because the
    connection stayed open while nothing arrived in a way the socket timeout
    caught. A watchdog closes the connection at the deadline, which makes the
    blocked read raise, and the caller gets ``TimeoutError`` as documented.
    900s default: a 35B model sharing llama.cpp with AIOS needs minutes for a
    full file; the old 120s produced TimeoutError on every real task.
    """
    payload = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False,
         "max_tokens": max_tokens, "temperature": 0.2}
    ).encode("utf-8")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("local LLM endpoint must be http on localhost")
    connection = HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
    expired = threading.Event()

    def _abort() -> None:
        expired.set()
        connection.close()  # unblocks a pending recv in the calling thread

    watchdog = threading.Timer(timeout, _abort)
    watchdog.daemon = True
    watchdog.start()
    try:
        connection.request("POST", "/v1/chat/completions", body=payload,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read().decode("utf-8")
    except (OSError, HTTPException) as exc:
        if expired.is_set():
            raise TimeoutError(f"local LLM call exceeded {timeout}s deadline") from exc
        raise
    finally:
        watchdog.cancel()
        connection.close()
    if expired.is_set():
        raise TimeoutError(f"local LLM call exceeded {timeout}s deadline")
    if response.status != 200:
        raise HTTPException(f"local LLM returned HTTP {response.status}: {body[:200]}")
    data = json.loads(body)
    return str(data["choices"][0]["message"]["content"])
