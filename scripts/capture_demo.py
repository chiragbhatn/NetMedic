"""Capture the README / portfolio screenshots, demo video and GIF of the NetMedic web UI.

What it does
------------
Starts ``netmedic web`` on a free local port, drives Chromium with Playwright, and writes
every asset into ``docs/media/`` (see the README there). It runs three real diagnoses:

1. ``srl_interface_down`` at 1440x900: idle / mid-run / report / full-page stills, plus
   frames for the GIF. The finished run is reloaded through its ``#run=`` hash for the
   light-theme and mobile stills, which costs no model call.
2. ``srl_interface_down`` at 1280x720 with video recording: ``web-ui-demo.webm``.
3. ``injection``: ``web-ui-injection.png``.

Cost: THE MODEL CALLS ARE REAL. Each diagnosis spends Anthropic API credit (about $0.02).
``--max-runs`` (default 3, hard cap 4) refuses to start more runs than that.

Setup (once, inside the project virtualenv; not part of pyproject.toml):

    .venv\\Scripts\\python -m pip install playwright pillow
    .venv\\Scripts\\python -m playwright install chromium

Run from the project root so the app finds ``.env``, ``inventory.yaml`` and ``fixtures/``:

    .venv\\Scripts\\python scripts/capture_demo.py
"""

from __future__ import annotations

import argparse
import io
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "media"
SCENARIO = "srl_interface_down"
RUN_TIMEOUT_MS = 240_000
HARD_CAP = 4


class RunBudget:
    """Counts real diagnoses so a bug cannot spend more than the user allowed."""

    def __init__(self, limit: int):
        self.limit, self.used = limit, 0

    def spend(self, what: str) -> None:
        if self.used >= self.limit:
            raise SystemExit(f"--max-runs {self.limit} reached; refusing to start '{what}'")
        self.used += 1
        print(f"[run {self.used}/{self.limit}] starting real diagnosis: {what}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port: int) -> subprocess.Popen:
    exe = Path(sys.executable).parent / ("netmedic.exe" if sys.platform == "win32" else "netmedic")
    proc = subprocess.Popen(
        [str(exe), "web", "--scenario", SCENARIO, "--port", str(port)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 40
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"netmedic web exited early with code {proc.returncode}")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=1).read()
            return proc
        except OSError:
            time.sleep(0.5)
    raise SystemExit("netmedic web did not answer within 40 s")


def stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


# --- page helpers ----------------------------------------------------------------------------


def open_page(page: Page, url: str, scenario: str = SCENARIO) -> None:
    page.goto(url)
    page.wait_for_function("document.querySelectorAll('#scenario option').length > 0")
    page.wait_for_function("!document.getElementById('run').disabled")
    page.select_option("#scenario", scenario)
    settle(page)


def settle(page: Page, ms: int = 600) -> None:
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(ms)


def start_run(page: Page) -> str:
    page.click("#run")
    page.wait_for_function("/#run=/.test(location.hash)")
    return re.search(r"#run=(\w+)", page.url).group(1)


def wait_report(page: Page) -> None:
    page.wait_for_selector("#report:not([hidden])", timeout=RUN_TIMEOUT_MS)
    page.wait_for_function("document.getElementById('run').disabled === false")
    settle(page, 1800)  # let the smooth scroll to the report finish


def assert_clean(page: Page) -> None:
    if not page.locator("#failure").is_hidden():
        raise SystemExit("error panel visible: " + page.inner_text("#failure"))


def scroll_to_report(page: Page) -> None:
    page.evaluate("document.getElementById('report').scrollIntoView({block: 'start'})")
    page.wait_for_timeout(500)


def jpeg_frame(page: Page) -> Image.Image:
    return Image.open(io.BytesIO(page.screenshot(scale="css"))).convert("RGB")


def build_gif(frames: list[Image.Image], path: Path) -> None:
    """Even subset of the frames, 960 px wide, ~10 s, last frame held on the report."""
    for count in (36, 30, 24, 18):
        step = max(1, len(frames) // count)
        picked = frames[::step][: count - 1] + [frames[-1]]
        small = [f.resize((960, round(f.height * 960 / f.width)), Image.LANCZOS) for f in picked]
        pal = [
            f.quantize(colors=96, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
            for f in small
        ]
        durations = [260] * (len(pal) - 1) + [2200]
        pal[0].save(
            path, save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=True
        )
        size = path.stat().st_size
        print(f"gif: {len(pal)} frames, {size / 1e6:.1f} MB", flush=True)
        if size < 6_000_000:
            return
    raise SystemExit("could not get the GIF under 6 MB")


# --- the captures ----------------------------------------------------------------------------


def main_run(browser, url: str, budget: RunBudget) -> str:
    """Run 1: idle, mid-run, report and full-page stills, GIF frames."""
    ctx = browser.new_context(
        bypass_csp=True,
        viewport={"width": 1440, "height": 900},
        device_scale_factor=2,
        color_scheme="dark",
    )
    page = ctx.new_page()
    open_page(page, url)
    page.screenshot(path=OUT / "web-ui-idle.png")
    frames = [jpeg_frame(page)]

    budget.spend(SCENARIO + " (stills)")
    run_id = start_run(page)
    running_done = False
    deadline = time.time() + RUN_TIMEOUT_MS / 1000
    while page.locator("#report").is_hidden():
        if time.time() > deadline:
            raise SystemExit("run timed out")
        cards = page.locator("#timeline > li").count()
        if not running_done and cards >= 4:
            settle(page, 900)
            if page.locator("#report").is_hidden():
                page.screenshot(path=OUT / "web-ui-running.png")
                running_done = True
        frames.append(jpeg_frame(page))
        page.wait_for_timeout(700)
    wait_report(page)
    assert_clean(page)
    if not running_done:
        print("WARNING: run finished before the mid-run still was taken", flush=True)
    frames.append(jpeg_frame(page))
    page.screenshot(path=OUT / "web-ui-report.png")
    page.screenshot(path=OUT / "web-ui-full.png", full_page=True)
    build_gif(frames, OUT / "web-ui-demo.gif")
    ctx.close()
    return run_id


def reload_stills(browser, url: str, run_id: str) -> None:
    """Re-open the finished run through its hash: no model call."""
    ctx = browser.new_context(
        bypass_csp=True,
        viewport={"width": 1440, "height": 900},
        device_scale_factor=2,
        color_scheme="light",
    )
    page = ctx.new_page()
    page.goto(f"{url}#run={run_id}")
    wait_report(page)
    assert_clean(page)
    scroll_to_report(page)
    page.screenshot(path=OUT / "web-ui-report-light.png")
    ctx.close()

    ctx = browser.new_context(
        bypass_csp=True,
        viewport={"width": 390, "height": 844},
        device_scale_factor=2,
        color_scheme="dark",
        is_mobile=True,
        has_touch=True,
    )
    page = ctx.new_page()
    page.goto(f"{url}#run={run_id}")
    wait_report(page)
    assert_clean(page)
    page.evaluate("window.scrollTo(0, 0)")
    settle(page, 500)
    page.screenshot(path=OUT / "web-ui-mobile.png", full_page=True)
    ctx.close()


def video_run(browser, url: str, budget: RunBudget, tmp: Path) -> None:
    """Run 2: one continuous 1280x720 recording of a complete run."""
    ctx = browser.new_context(
        bypass_csp=True,
        viewport={"width": 1280, "height": 720},
        color_scheme="dark",
        record_video_dir=str(tmp),
        record_video_size={"width": 1280, "height": 720},
    )
    page = ctx.new_page()
    open_page(page, url)
    page.wait_for_timeout(2500)
    budget.spend(SCENARIO + " (video)")
    start_run(page)
    wait_report(page)
    assert_clean(page)
    scroll_to_report(page)
    page.wait_for_timeout(3000)
    video = page.video
    ctx.close()  # finalises the file
    shutil.move(video.path(), OUT / "web-ui-demo.webm")


def injection_run(browser, url: str, budget: RunBudget) -> None:
    """Run 3: the prompt-injection scenario; expand the card with the hostile text."""
    ctx = browser.new_context(
        bypass_csp=True,
        viewport={"width": 1440, "height": 900},
        device_scale_factor=2,
        color_scheme="dark",
    )
    page = ctx.new_page()
    open_page(page, url, "injection")
    problem = page.input_value("#problem")
    if problem.strip() != "host1 cannot reach host2":
        print(f"note: injection problem text is {problem!r}", flush=True)
    budget.spend("injection")
    start_run(page)
    wait_report(page)
    assert_clean(page)
    card = page.locator("#timeline > li", has_text="IGNORE ALL PREVIOUS INSTRUCTIONS")
    if card.count():
        card = card.first
        card.locator("summary").click()
        card.scroll_into_view_if_needed()
        card.evaluate("n => n.scrollIntoView({block: 'center'})")
        settle(page, 800)
        print("injection: captured the card containing the hostile text", flush=True)
    else:
        scroll_to_report(page)
        print(
            "injection: NO card contains the hostile text; captured the report instead", flush=True
        )
    page.screenshot(path=OUT / "web-ui-injection.png")
    ctx.close()


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--max-runs",
        type=int,
        default=3,
        help=f"most real diagnoses to start (default 3, hard cap {HARD_CAP})",
    )
    args = ap.parse_args()
    budget = RunBudget(min(args.max_runs, HARD_CAP))
    if budget.limit < 3:
        print(
            "note: 3 runs are needed for every asset; the capture will stop when the budget runs out"
        )
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="netmedic-video-"))
    port = free_port()
    url = f"http://127.0.0.1:{port}/"
    server = start_server(port)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                run_id = main_run(browser, url, budget)
                reload_stills(browser, url, run_id)
                video_run(browser, url, budget, tmp)
                injection_run(browser, url, budget)
            finally:
                browser.close()
    finally:
        stop_server(server)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"done; {budget.used} real diagnoses started")


if __name__ == "__main__":
    main()
