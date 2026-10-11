# Operations

## Invocation and output

Python 3.10+ works on Windows, macOS and Linux. `python scripts/generate.py --help`
lists all options. All example paths are relative to the skill directory.

```sh
python scripts/generate.py --prompt-file /path/prompt.txt --size portrait --style watercolor
python scripts/generate.py --task-id EXISTING_ID --transport auto
python scripts/generate.py "A mountain lake" --transport http
python scripts/generate.py "A mountain lake" --proxy http://127.0.0.1:7890
```

Success JSON includes `file_path`, `mime_type`, `size_bytes`, `transport`, `task_id`,
`poll_count`, and `fallback_reason`. Diagnostics go to stderr. Exit 0 means a file
was saved; exit 1 is an operational error; exit 2 is invalid CLI input. `retryable`
in error JSON indicates whether a different transport/resume may help, not a
guarantee. Outputs have unique names, are never overwritten, and are not deleted
automatically. The default output directory is `imagefree-output` under the caller's
working directory. Returned image signatures are checked so HTML errors are not
saved as images; this is not a complete image decoder.

## Browser and network

`IMAGEFREE_PROXY`, `IMAGEFREE_BROWSER_EXECUTABLE`, and `IMAGEFREE_BROWSER_PROFILE`
provide defaults overridden by the corresponding CLI arguments. Use `--proxy`
or `IMAGEFREE_PROXY` to route both transports consistently. HTTP also honors the
standard urllib proxy environment variables; Playwright does not inherit those.
An explicit HTTP(S) proxy must include a port. Proxy credentials are not printed.

Browser fallback launches Playwright Chromium with a dedicated persistent profile,
loads the homepage, and executes API fetches inside the page, as in the original
plugin. Image downloads without cross-origin CORS support use the same browser
context's request client. It does not solve CAPTCHAs or evade access controls.
Use `--headed --verification-wait 30` on a desktop to allow manual verification
before the browser request. If a challenge remains, stop and report it; a browser
does not guarantee access. `--headed` requires a graphical session. Do not run two
processes against the same profile simultaneously; use distinct profile paths.

`--timeout` defaults to 180 seconds per transport; `--request-timeout` defaults to
45 seconds per network operation; `--poll-interval` defaults to 3 seconds. Browser
launch/homepage load and optional manual verification add time outside the generation
budget. HTTP read timeouts are socket timeouts, not a hard wall-clock limit on slow
streaming responses. The script caps accepted response bodies at 32 MiB (the browser
request-client CORS fallback checks after buffering).

## Service contract

This is a client of the website's internal API, not an official stable Imagefree SDK.
It sends `POST /api/generate` with `prompt`, `aspect_ratio`, and `turnstile_token: null`,
then polls `GET /api/generate/status?taskId=...`. It supports `taskId`/`task_id`/`id`
and nested `data`/`result`/`output`; results can be raster bytes, known image URL fields,
data URLs or `base64`/`b64_json`. It does not download arbitrary strings found in the
response. Prompts and style hints are sent to Imagefree; keep secrets out of prompts.

Known task IDs and image URLs survive the HTTP-to-browser handoff. A timeout after
submission does not create a new task. An ambiguous POST failure stops because the
server may already be generating the image. A response with no usable task or image
cannot be claimed as successful. HTTP 400/401/402/422 and explicit task errors stop;
other HTTP failures allow one browser attempt. If the backend begins requiring a
verification token or changes its API schema, report that limitation rather than
inventing a token or repeatedly submitting requests.

## Development verification

From the repository root: `python -m unittest discover -s tests -v`.
Tests use local fixtures and do not consume live generation requests. Set
`IMAGEFREE_TEST_BROWSER=1` to include a real Playwright browser fallback test against
a local HTTP server. Live provider availability is separate from fixture validation.
