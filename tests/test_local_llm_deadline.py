"""The local LLM client enforces a total deadline, even against a server that never answers."""
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from smart_store_control.local_llm import complete


class _Silent(BaseHTTPRequestHandler):
    """Accepts the request, then sends nothing until told to stop (a queued llama.cpp request)."""
    release = threading.Event()

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.release.wait(10)
        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")
        except OSError:
            pass

    def log_message(self, *args):  # noqa: D401
        pass


class _Answering(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"choices": [{"message": {"content": "READY"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class LocalLlmDeadlineTests(unittest.TestCase):
    def _serve(self, handler):
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def test_deadline_fires_when_the_server_stays_silent(self):
        endpoint = self._serve(_Silent)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            complete("hello", endpoint=endpoint, model="m", timeout=1)
        self.assertLess(time.monotonic() - started, 5)
        _Silent.release.set()

    def test_normal_reply_is_returned_and_watchdog_is_cancelled(self):
        endpoint = self._serve(_Answering)
        started = time.monotonic()
        self.assertEqual("READY", complete("hello", endpoint=endpoint, model="m", timeout=5))
        self.assertLess(time.monotonic() - started, 2)  # returned well before the deadline, watchdog did not fire

    def test_non_local_endpoint_is_refused(self):
        with self.assertRaises(ValueError):
            complete("hello", endpoint="http://10.0.0.5:8081", model="m", timeout=1)


if __name__ == "__main__":
    unittest.main()
