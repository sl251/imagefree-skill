import base64
from contextlib import contextmanager
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "skills/imagefree/scripts/generate.py"
spec = importlib.util.spec_from_file_location("imagefree_generate", SCRIPT)
g = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = g
spec.loader.exec_module(g)
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=")


def response(data, status=200):
    return g.Response(status, "application/json", json.dumps(data).encode())


def args(*extra):
    result = g.parser().parse_args(["a cabin", *extra])
    result.aspect_ratio = result.aspect_ratio or g.RATIOS[result.size]
    return result


class FakeTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def request(self, method, url, payload=None, timeout=None):
        self.calls.append((method, url, payload))
        item = next(self.responses)
        if isinstance(item, Exception):
            raise item
        return item


class WorkflowTests(unittest.TestCase):
    def test_http_success_never_starts_browser(self):
        http = FakeTransport([response({"data": {"b64_json": base64.b64encode(PNG).decode()}})])
        def forbidden(_):
            self.fail("browser launched on HTTP success")
        image, transport, reason, _ = g.generate(args(), http_factory=lambda _: http, browser_factory=forbidden)
        self.assertEqual((image, transport, reason), (PNG, "http", None))
        self.assertTrue(http.closed)

    def test_blocked_submit_falls_back_once(self):
        http = FakeTransport([g.Response(429, "text/html", b"challenge")])
        browser = FakeTransport([g.Response(200, "image/png", PNG)])
        result = g.generate(args(), http_factory=lambda _: http, browser_factory=lambda _: browser)
        self.assertEqual(result[1], "browser")
        self.assertIn("429", result[2])
        self.assertEqual([c[0] for c in browser.calls], ["POST"])

    def test_poll_fallback_resumes_task_without_post(self):
        http = FakeTransport([response({"taskId": "task/one"}), g.Response(403, "text/html", b"blocked")])
        browser = FakeTransport([response({"data": {"imageUrl": "/image"}}), g.Response(200, "image/png", PNG)])
        result = g.generate(args(), http_factory=lambda _: http, browser_factory=lambda _: browser)
        self.assertEqual(result[0], PNG)
        self.assertEqual(result[3].task_id, "task/one")
        self.assertEqual([c[0] for c in browser.calls], ["GET", "GET"])
        self.assertIn("task%2Fone", browser.calls[0][1])

    def test_download_fallback_does_not_poll_or_submit(self):
        http = FakeTransport([response({"image_url": "/image"}), g.GenerationError("download failed")])
        browser = FakeTransport([g.Response(200, "image/png", PNG)])
        g.generate(args(), http_factory=lambda _: http, browser_factory=lambda _: browser)
        self.assertEqual(len(browser.calls), 1)
        self.assertTrue(browser.calls[0][1].endswith("/image"))

    def test_terminal_failure_never_retries(self):
        for failure in [response({"status": "failed"}), response({"error": "quota"}),
                        response({}, 422), g.TerminalError("POST may have been accepted")]:
            with self.subTest(failure=failure):
                http = FakeTransport([failure])
                with self.assertRaises(g.TerminalError):
                    g.generate(args(), http_factory=lambda _: http,
                               browser_factory=lambda _: self.fail("unexpected retry"))

    def test_accepted_without_id_is_not_resubmitted(self):
        http = FakeTransport([response({"message": "accepted"})])
        browser = FakeTransport([])
        with self.assertRaises(g.TerminalError):
            g.generate(args(), http_factory=lambda _: http, browser_factory=lambda _: browser)
        self.assertEqual(browser.calls, [])

    def test_nested_image_id_does_not_replace_task_id(self):
        state = g.State(task_id="original-task")
        g.decode_response(response({"data": {"id": "image-id", "image_url": "/image"}}), state, g.HOME)
        self.assertEqual(state.task_id, "original-task")

    def test_http_only_error_does_not_launch_browser(self):
        http = FakeTransport([response({}, 503)])
        with self.assertRaises(g.GenerationError):
            g.generate(args("--transport", "http"), http_factory=lambda _: http,
                       browser_factory=lambda _: self.fail("unexpected browser"))

    def test_resume_skips_generation(self):
        http = FakeTransport([g.Response(200, "image/png", PNG)])
        g.generate(args("--task-id", "existing"), http_factory=lambda _: http)
        self.assertEqual(http.calls[0][0], "GET")

    def test_rejects_html_masquerading_as_image(self):
        with self.assertRaises(g.GenerationError):
            g.decode_response(g.Response(200, "image/png", b"<html>error</html>"), g.State(), g.HOME)

    def test_does_not_download_echoed_prompt_url(self):
        state = g.State()
        self.assertIsNone(g.decode_response(response({"taskId": "abc", "prompt": "https://example.org/a.png"}), state, g.HOME))
        self.assertIsNone(state.image_url)

    def test_invalid_base64_rejected(self):
        with self.assertRaises(g.GenerationError):
            g.decode_response(response({"b64_json": "###"}), g.State(), g.HOME)

    def test_poll_budget_is_bounded(self):
        http = FakeTransport([response({"taskId": "abc"})])
        with patch.object(g.time, "monotonic", side_effect=[0, 0, 2]):
            with self.assertRaisesRegex(g.GenerationError, "timed out"):
                g.run_transport(http, args("--timeout", "1"), g.State())

    def test_numeric_options_reject_nan_infinity_and_zero(self):
        for value in ("nan", "inf", "0", "-1"):
            with self.subTest(value=value), self.assertRaises(SystemExit), patch("sys.stderr", io.StringIO()):
                args("--timeout", value)


@contextmanager
def server(block_http=False):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, content_type, body):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(("POST", self.path, payload))
            if block_http and self.headers.get("User-Agent") == "imagefree-skill/1.0":
                self.send(429, "text/html", b"challenge")
            else:
                self.send(200, "application/json", b'{"taskId":"fixture-task"}')

        def do_GET(self):
            calls.append(("GET", self.path, None))
            if self.path.startswith("/api/generate/status"):
                self.send(200, "application/json", b'{"status":"completed","data":{"image_url":"/image"}}')
            elif self.path == "/image":
                self.send(200, "image/png", PNG)
            else:
                self.send(200, "text/html", b"<!doctype html><title>Imagefree test fixture</title>")

    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=instance.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{instance.server_port}/", calls
    finally:
        instance.shutdown()
        instance.server_close()
        worker.join()


class IntegrationTests(unittest.TestCase):
    def test_real_http_cli_saves_image_and_json(self):
        with server() as (url, calls), tempfile.TemporaryDirectory() as temp:
            prompt_file = Path(temp) / "prompt.txt"
            prompt_file.write_text("a cabin with 'quotes' and $dollars\nwatercolor", encoding="utf-8")
            result = subprocess.run([sys.executable, str(SCRIPT), "--prompt-file", str(prompt_file),
                                     "--home-url", url, "--transport", "http", "--size", "tall",
                                     "--output-dir", temp], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(Path(data["file_path"]).read_bytes(), PNG)
            self.assertEqual(data["transport"], "http")
            self.assertEqual(calls[0][2]["aspect_ratio"], "9:16")
            self.assertEqual(calls[0][2]["prompt"], prompt_file.read_text())

    @unittest.skipUnless(os.environ.get("IMAGEFREE_TEST_BROWSER") == "1", "optional real browser test")
    def test_real_browser_after_http_block(self):
        with server(block_http=True) as (url, calls), tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, str(SCRIPT), "a cabin", "--home-url", url,
                                     "--browser-profile", str(Path(temp) / "profile"),
                                     "--output-dir", temp, "--timeout", "15"],
                                    capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["transport"], "browser")
            self.assertEqual(Path(data["file_path"]).read_bytes(), PNG)
            self.assertIn("429", data["fallback_reason"])
            self.assertEqual(len([c for c in calls if c[0] == "POST"]), 2)


if __name__ == "__main__":
    unittest.main()
