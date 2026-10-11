#!/usr/bin/env python3
"""Imagefree text-to-image: standard-library HTTP, then optional Playwright."""
from __future__ import annotations

import argparse
import base64
import binascii
import http.cookiejar
import json
import math
import os
from pathlib import Path
import shutil
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

HOME = "https://imagefree.net/"
RATIOS = {"square": "1:1", "portrait": "3:4", "landscape": "4:3", "tall": "9:16", "wide": "16:9"}
MAX_BYTES = 32 * 1024 * 1024


class GenerationError(Exception):
    """A retry with a different transport may help."""


class TerminalError(GenerationError):
    """Do not resubmit rejected, failed, or ambiguously accepted requests."""


@dataclass
class State:
    task_id: str | None = None
    image_url: str | None = None
    poll_count: int = 0
    submitted: bool = False


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes


def image_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise GenerationError("Response is not a supported raster image (PNG/JPEG/WebP/GIF).")


def validate_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise GenerationError("Expected an HTTP(S) URL without embedded credentials.")
    return url


class HttpTransport:
    def __init__(self, args):
        self.args = args
        handlers = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]
        if args.proxy:
            handlers.append(urllib.request.ProxyHandler({"http": args.proxy, "https": args.proxy}))
        self.opener = urllib.request.build_opener(*handlers)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, url, payload=None, timeout=None):
        validate_url(url)
        headers = {"Accept": "application/json, image/*", "User-Agent": "imagefree-skill/1.0"}
        if urllib.parse.urlsplit(url).netloc == urllib.parse.urlsplit(self.args.home_url).netloc:
            headers["Referer"] = self.args.home_url
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            try:
                response = self.opener.open(request, timeout=timeout or self.args.request_timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise GenerationError("Response exceeds the 32 MiB limit.")
                return Response(response.status, response.headers.get("Content-Type", ""), body)
        except (OSError, urllib.error.URLError) as exc:
            cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            not_delivered = isinstance(cause, (socket.gaierror, ConnectionRefusedError))
            error = TerminalError if method == "POST" and not not_delivered else GenerationError
            raise error(f"{method} transport failed ({type(exc).__name__}); "
                        + ("submission may have been accepted; do not automatically resubmit." if error is TerminalError
                           else "browser fallback can resume the task.")) from exc


BROWSER_FETCH = r"""async ({method, url, payload, timeout, limit}) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, {
      method, credentials: 'same-origin', signal: controller.signal,
      ...(payload === null ? {} : {
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
      })
    });
    const reader = response.body.getReader();
    const chunks = []; let length = 0;
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      length += value.length;
      if (length > limit) { await reader.cancel(); throw new Error('Response exceeds 32 MiB'); }
      chunks.push(value);
    }
    let binary = '';
    for (const chunk of chunks) {
      for (let i = 0; i < chunk.length; i += 32768)
        binary += String.fromCharCode(...chunk.subarray(i, i + 32768));
    }
    return {status: response.status, content_type: response.headers.get('content-type') || '',
            body: btoa(binary)};
  } finally { clearTimeout(timer); }
}"""


def find_browser():
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge"):
        if path := shutil.which(name):
            return path
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    ]
    return next((str(p) for p in candidates if p.is_file()), None)


class BrowserTransport:
    def __init__(self, args):
        self.args = args
        self.runtime = None
        self.context = None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise GenerationError("Browser fallback requires: python -m pip install playwright; "
                                  "python -m playwright install chromium (or --browser-executable PATH).") from exc
        try:
            self.runtime = sync_playwright().start()
            options = {"headless": not self.args.headed}
            executable = self.args.browser_executable or find_browser()
            if executable:
                options["executable_path"] = executable
            if self.args.proxy:
                parsed = urllib.parse.urlsplit(self.args.proxy)
                options["proxy"] = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
                if parsed.username:
                    options["proxy"].update(username=urllib.parse.unquote(parsed.username),
                                            password=urllib.parse.unquote(parsed.password or ""))
            self.context = self.runtime.chromium.launch_persistent_context(
                str(Path(self.args.browser_profile).expanduser().resolve()), **options)
            self.page = self.context.new_page()
            self.page.goto(self.args.home_url, wait_until="domcontentloaded",
                           timeout=self.args.request_timeout * 1000)
            if self.args.headed and self.args.verification_wait:
                print("Waiting for manual browser verification...", file=sys.stderr)
                self.page.wait_for_timeout(self.args.verification_wait * 1000)
            return self
        except Exception as exc:
            self.__exit__()
            raise GenerationError(f"Browser startup failed ({type(exc).__name__}). Check Playwright, "
                                  "browser executable, proxy and profile; use --headed for manual verification.") from exc

    def __exit__(self, *exc):
        try:
            if self.context:
                self.context.close()
        finally:
            if self.runtime:
                self.runtime.stop()
        return False

    def request(self, method, url, payload=None, timeout=None):
        validate_url(url)
        try:
            result = self.page.evaluate(BROWSER_FETCH, {
                "method": method, "url": url, "payload": payload, "limit": MAX_BYTES,
                "timeout": int((timeout or self.args.request_timeout) * 1000),
            })
            return Response(result["status"], result["content_type"], base64.b64decode(result["body"]))
        except Exception as exc:
            # Cross-origin images may lack CORS; the context request client shares cookies.
            if method == "GET" and urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(self.args.home_url).netloc:
                try:
                    response = self.context.request.get(url, timeout=(timeout or self.args.request_timeout) * 1000)
                    try:
                        body = response.body()
                        if len(body) > MAX_BYTES:
                            raise GenerationError("Response exceeds the 32 MiB limit.")
                        return Response(response.status, response.headers.get("content-type", ""), body)
                    finally:
                        response.dispose()
                except Exception:
                    pass
            error = TerminalError if method == "POST" else GenerationError
            raise error(f"Browser {method} failed ({type(exc).__name__}). "
                        "If a challenge is shown, complete it manually; do not repeatedly resubmit.") from exc


def nodes(value):
    if isinstance(value, dict):
        yield value
        for key in ("data", "result", "output", "images"):
            yield from nodes(value.get(key))
    elif isinstance(value, list):
        for item in value:
            yield from nodes(item)


def image_candidate(value):
    if isinstance(value, str):
        if value.startswith(("http://", "https://", "/", "data:image/")):
            return value
    elif isinstance(value, list):
        for item in value:
            if candidate := image_candidate(item):
                return candidate
    elif isinstance(value, dict):
        for key in ("image_url", "imageUrl", "url", "image", "images", "b64_json", "base64", "output", "result", "data"):
            item = value.get(key)
            if key in {"b64_json", "base64"} and isinstance(item, str):
                return "data:image/png;base64," + item
            if candidate := image_candidate(item):
                return candidate
    return None


def decode_response(response, state, home_url):
    if not 200 <= response.status < 300:
        # Validation/auth/payment errors are not repaired by changing transports.
        error = TerminalError if response.status in {400, 401, 402, 422} else GenerationError
        raise error(f"Service returned HTTP {response.status}.")
    if response.content_type.lower().startswith("image/"):
        image_type(response.body)
        return response.body
    try:
        data = json.loads(response.body)
    except (ValueError, UnicodeError) as exc:
        raise GenerationError("Service returned non-JSON content (possibly a browser challenge).") from exc
    for node in nodes(data):
        for key in ("taskId", "task_id", "id"):
            if not state.task_id and isinstance(node.get(key), (str, int)) and node[key]:
                state.task_id = str(node[key])
                break
        status = str(node.get("status", node.get("state", node.get("phase", "")))).lower()
        if status in {"failed", "failure", "error", "cancelled", "canceled"} or node.get("error"):
            raise TerminalError("Service reported a failed or rejected generation task.")
    candidate = image_candidate(data)
    if candidate:
        if candidate.startswith("data:"):
            try:
                header, encoded = candidate.split(",", 1)
                if ";base64" not in header:
                    raise ValueError("not base64")
                image = base64.b64decode(encoded, validate=True)
                if len(image) > MAX_BYTES:
                    raise ValueError("image too large")
                image_type(image)
                return image
            except (ValueError, binascii.Error) as exc:
                raise GenerationError("Service returned invalid base64 image data.") from exc
        state.image_url = validate_url(urllib.parse.urljoin(home_url, candidate))
    return None


def run_transport(transport, args, state):
    deadline = time.monotonic() + args.timeout

    def request(method, url, payload=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GenerationError("Generation timed out; resume with the returned task_id if available.")
        return transport.request(method, url, payload, timeout=min(args.request_timeout, remaining))

    if not state.task_id and not state.image_url:
        if state.submitted:
            raise TerminalError("Submission was accepted without a usable task ID; do not automatically resubmit.")
        response = request("POST", urllib.parse.urljoin(args.home_url, "/api/generate"), {
            "prompt": args.prompt, "aspect_ratio": args.aspect_ratio, "turnstile_token": None,
        })
        state.submitted = 200 <= response.status < 300 and "json" in response.content_type.lower()
        image = decode_response(response, state, args.home_url)
        if image is not None:
            return image
        if not state.task_id and not state.image_url:
            raise GenerationError("Generation response contained neither an image nor a task ID.")
    while True:
        if state.image_url:
            response = request("GET", state.image_url)
            if not 200 <= response.status < 300:
                raise GenerationError(f"Image download returned HTTP {response.status}.")
            image_type(response.body)
            return response.body
        state.poll_count += 1
        url = urllib.parse.urljoin(args.home_url, "/api/generate/status") + "?" + urllib.parse.urlencode({"taskId": state.task_id})
        image = decode_response(request("GET", url), state, args.home_url)
        if image is not None:
            return image
        if not state.image_url:
            time.sleep(min(args.poll_interval, max(0, deadline - time.monotonic())))


def generate(args, state=None, http_factory=HttpTransport, browser_factory=BrowserTransport):
    state = state if state is not None else State(task_id=args.task_id)
    fallback_reason = None
    if args.transport != "browser":
        try:
            with http_factory(args) as transport:
                return run_transport(transport, args, state), "http", None, state
        except TerminalError:
            raise
        except GenerationError as exc:
            if args.transport == "http":
                raise
            if state.submitted and not state.task_id and not state.image_url:
                raise TerminalError("Submission was accepted without a usable task ID; do not automatically resubmit.") from exc
            fallback_reason = str(exc)
            print("HTTP failed; trying browser once: " + fallback_reason, file=sys.stderr)
    with browser_factory(args) as transport:
        return run_transport(transport, args, state), "browser", fallback_reason, state


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return number


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("prompt", nargs="?", help="Text prompt; use --prompt-file for complex text")
    result.add_argument("--prompt-file", type=Path)
    result.add_argument("--style", default="")
    result.add_argument("--size", choices=RATIOS, default="square")
    result.add_argument("--aspect-ratio", choices=list(RATIOS.values()))
    result.add_argument("--transport", choices=("auto", "http", "browser"), default="auto")
    result.add_argument("--output-dir", type=Path, default=Path.cwd() / "imagefree-output")
    result.add_argument("--task-id", help="Resume an existing task without submitting a prompt")
    result.add_argument("--home-url", default=HOME)
    result.add_argument("--timeout", type=positive, default=180, help="Generation budget per transport in seconds")
    result.add_argument("--request-timeout", type=positive, default=45)
    result.add_argument("--poll-interval", type=positive, default=3)
    result.add_argument("--proxy", default=os.environ.get("IMAGEFREE_PROXY"), help="Explicit proxy shared by both transports")
    result.add_argument("--browser-executable", default=os.environ.get("IMAGEFREE_BROWSER_EXECUTABLE"))
    result.add_argument("--browser-profile", default=os.environ.get("IMAGEFREE_BROWSER_PROFILE", str(Path.home() / ".cache/imagefree-skill/browser-profile")))
    result.add_argument("--headed", action="store_true", help="Show browser; requires a graphical desktop")
    result.add_argument("--verification-wait", type=positive, default=0,
                        help="Seconds for manual verification before a headed browser request")
    return result


def main(argv=None):
    arg_parser = parser()
    args = arg_parser.parse_args(argv)
    state = State(task_id=args.task_id)
    try:
        if args.prompt_file:
            args.prompt = args.prompt_file.read_text(encoding="utf-8-sig")
        args.prompt = (args.prompt or "").strip()
        if not args.prompt and not args.task_id:
            arg_parser.error("provide a prompt, --prompt-file, or --task-id")
        if args.style and args.prompt:
            args.prompt += ", " + args.style
        args.aspect_ratio = args.aspect_ratio or RATIOS[args.size]
        validate_url(args.home_url)
        if args.proxy:
            proxy = urllib.parse.urlsplit(args.proxy)
            if proxy.scheme not in {"http", "https"} or not proxy.hostname or not proxy.port:
                raise GenerationError("--proxy requires an HTTP(S) proxy URL with an explicit port.")
        image, transport, reason, state = generate(args, state)
        mime, suffix = image_type(image)
        output = args.output_dir.expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        path = output / (uuid.uuid4().hex + suffix)
        with path.open("xb") as stream:
            stream.write(image)
        print(json.dumps({"status": "generated", "file_path": str(path), "mime_type": mime,
                          "size_bytes": len(image), "transport": transport, "task_id": state.task_id,
                          "poll_count": state.poll_count, "fallback_reason": reason}, ensure_ascii=False))
        return 0
    except (GenerationError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc), "task_id": state.task_id,
                          "retryable": not isinstance(exc, TerminalError)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
