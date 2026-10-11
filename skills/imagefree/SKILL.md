---
name: imagefree
description: Generate images from text using Imagefree.net with HTTP first and browser automation as fallback. Use when the user asks for Imagefree image generation or explicitly chooses this provider. Supports aspect ratios, style hints, saving local images, and resuming existing tasks without AstrBot.
---

# Imagefree

Use the bundled Python script from this skill's directory. It requires Python 3.10+;
HTTP uses only the standard library. Browser fallback uses optional Playwright.
Resolve the script path from this file's location, not the caller's working directory.

## Generate

1. Preserve the requested subject and style. Use `square` unless the user specifies
   a format: `portrait` (3:4), `landscape` (4:3), `tall` (9:16), `wide` (16:9).
2. Run one command with the default `auto` transport:

   ```sh
   python scripts/generate.py "A small cabin beside a lake, watercolor" --size wide --output-dir /absolute/output/path
   ```

   For long prompts or shell-sensitive text, write a UTF-8 file using a file tool
   and pass `--prompt-file /absolute/prompt.txt`; do not interpolate user text into
   shell code. `--style` appends a style hint; `--aspect-ratio` overrides the preset.
3. Read the final JSON on stdout. On `status: generated`, show/attach `file_path`
   with the host's supported local-image display mechanism. Include the absolute
   file link if attachments are unavailable. Only claim success after the file exists.
4. On an error, report it accurately. An existing `task_id` can be resumed with
   `--task-id ID`; do not submit the prompt again just to retry a poll or download.

## Transport behavior

`auto` tries HTTP and then starts the browser at most once on a recoverable failure.
It carries the task ID and download URL forward. Explicit task failure, invalid
requests, authentication/payment rejection, and a POST whose delivery is uncertain
stop the run; changing transports must not silently create a duplicate task.
`--transport http` and `--transport browser` are available for diagnostics or an
explicit user preference. The default remains HTTP first.

If browser support is missing, install it in the selected Python environment:

```sh
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
```

The requirements file is beside this `SKILL.md`. A system Chrome/Edge/Chromium is
also detected automatically; `--browser-executable PATH` overrides discovery.
The isolated browser profile defaults to `~/.cache/imagefree-skill/browser-profile`.
Do not use the user's personal browser profile or commit cookies/profile files.

For proxy configuration, manual verification, response details, and testing, read
[references/operations.md](references/operations.md). Do not silently switch to a
paid service or another provider if Imagefree fails.
