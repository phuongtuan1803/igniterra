"""Ánh xạ lỗi của adapter OmniRoute với server HTTP giả (T04 ở tầng adapter; 05 · Ánh xạ adapter OmniRoute)."""

from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from studio.providers.base import ContentRefused, Fatal, QualityFail, Retryable, Unconfirmed
from studio.providers.direct.omniroute import OmniRouteImage, OmniRouteLLM

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class Handler(BaseHTTPRequestHandler):
    mode = "ok"

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        mode = Handler.mode
        if mode == "timeout":
            time.sleep(2)
        status, body = {
            "429": (429, {"error": "rate"}), "500": (500, {"error": "boom"}), "401": (401, {"error": "key"}),
            "moderation": (400, {"error": {"message": "Gemini blocked this request through content moderation."}}),
            "nogen": (400, {"error": {"message": "Gemini could not generate an image (STOP)", "type": "upstream_error"}}),
            "badjson": (200, {"model": "gpt-5.6-luna", "choices": [{"message": {"content": "not json"}}]}),
            "othermodel": (200, {"model": "gpt-4o", "choices": [{"message": {"content": "{}"}}]}),
            "noimage": (200, {"created": 1, "data": []}),
        }.get(mode, (200, None))
        if body is None:
            body = ({"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode(), "media_type": "image/png"}]}
                    if "images" in self.path else
                    {"model": "gpt-5.6-luna", "usage": {"prompt_tokens": 3, "completion_tokens": 4},
                     "choices": [{"message": {"content": json.dumps({"a": 1})}}]})
        data = json.dumps(body).encode()
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except BrokenPipeError:   # client đã bỏ đi vì timeout
            pass


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    httpd.shutdown()


@pytest.fixture
def config(server, monkeypatch):
    monkeypatch.setenv("OMNIROUTE_API_KEY", "test-key")
    return {"omniroute": {"base_url": server, "llm_model": "codex/gpt-5.6-luna", "image_model": "openrouter/x/img",
                          "image_max_refs": 1, "timeout_s": 0.5}}


@pytest.mark.parametrize("mode, error", [("429", Retryable), ("500", Unconfirmed), ("401", Fatal), ("timeout", Unconfirmed),
                                         ("badjson", QualityFail), ("othermodel", Fatal)])
def test_llm_errors(config, mode, error):
    Handler.mode = mode
    with pytest.raises(error):
        OmniRouteLLM(config).generate_json("x", {"type": "object"}, "t")


def test_llm_ok(config):
    Handler.mode = "ok"
    assert OmniRouteLLM(config).generate_json("x", {"type": "object"}, "t") == {"a": 1}


@pytest.mark.parametrize("mode, error", [("moderation", ContentRefused), ("nogen", ContentRefused), ("noimage", Unconfirmed), ("500", Unconfirmed)])
def test_image_errors(config, tmp_path, mode, error):
    Handler.mode = mode
    with pytest.raises(error):
        OmniRouteImage(config).generate("p", [], "1080x1920", tmp_path / "out")


def test_image_ok_with_ref(config, tmp_path):
    Handler.mode = "ok"
    ref = tmp_path / "sheet.png"
    ref.write_bytes(PNG)
    candidate = OmniRouteImage(config).generate("p", [ref], "1080x1920", tmp_path / "out")
    assert candidate.path.endswith(".png") and candidate.model == "openrouter/x/img"


def test_connection_refused_is_retryable(config, tmp_path):
    config["omniroute"]["base_url"] = "http://127.0.0.1:9/v1"
    with pytest.raises(Retryable):
        OmniRouteImage(config).generate("p", [], "1080x1920", tmp_path / "out")
