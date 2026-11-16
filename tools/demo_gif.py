"""Record the README's 60-second demo GIF from the real app: Demo mode replays NASA IMS test 2 bearing 1.

    python -m tools.demo_gif [--port 8611] [--out docs/media/demo.gif]

Starts the server (no MIMII replay, no browser window), drives the installed Google Chrome headless
with Playwright, clicks Demo mode -> Start (no phone number, so WhatsApp runs as a dry run unless .env
has keys AND DHADKAN_DEMO_TO is set), opens Loom 1, screenshots twice a second until the IMS bearing
fails, ends on the Log drawer, and writes an optimised GIF with Pillow. Nothing in the frames is staged.
"""

import argparse
import io
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
FAKE_TO = "+91 90000 00000"  # never a real number; only ever used with the dry-run provider


def wait_up(url: str, timeout: float = 60) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return
        except OSError:
            time.sleep(0.5)
    raise SystemExit(f"server did not come up at {url}")


def to_gif(frames: list[tuple[bytes, float]], out: Path, width: int) -> None:
    imgs, durs = [], []
    for i, (png, t) in enumerate(frames):
        im = Image.open(io.BytesIO(png)).convert("RGB")
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        imgs.append(im.quantize(colors=96, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE))
        nxt = frames[i + 1][1] if i + 1 < len(frames) else t + 2.5
        durs.append(max(60, round((nxt - t) * 1000)))
    out.parent.mkdir(parents=True, exist_ok=True)
    imgs[0].save(out, save_all=True, append_images=imgs[1:], duration=durs, loop=0, optimize=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8611)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "media" / "demo.gif")
    ap.add_argument("--width", type=int, default=880)
    ap.add_argument("--fps", type=float, default=2.0)
    ap.add_argument("--max-s", type=float, default=90.0)
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    base = f"http://127.0.0.1:{args.port}"
    # never send from a recording: drop any WhatsApp/Twilio keys so the provider is the dry run
    env = {k: v for k, v in os.environ.items() if not k.startswith(("WA_", "TWILIO_"))}
    env.update(PYTHONUNBUFFERED="1", DHADKAN_DRY_RUN="1")
    srv = subprocess.Popen([sys.executable, "-m", "server.demo", "--port", str(args.port), "--no-replay", "--no-open"],
                           cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)  # fmt: skip
    frames: list[tuple[bytes, float]] = []
    try:
        wait_up(base + "/api/state")
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True, args=["--use-angle=metal"])
            page = browser.new_page(viewport={"width": 1440, "height": 860}, device_scale_factor=1)
            page.goto(base + "/?view=cluster&m=A-G1")  # the cluster screen, where Demo mode lives
            page.wait_for_timeout(4000)
            t0 = time.monotonic()

            def shot() -> None:
                frames.append((page.screenshot(type="png"), time.monotonic() - t0))

            shot()
            page.get_by_role("button", name="Demo mode", exact=True).click()
            page.wait_for_timeout(600)
            # an obviously fake number: with no keys in .env the alert is built and shown as a dry run
            page.locator(".demo input").fill(FAKE_TO)
            page.locator(".demo select").select_option("hi")
            shot()
            page.get_by_role("button", name=re.compile("Start Demo mode")).click()
            page.wait_for_timeout(1200)
            shot()
            page.get_by_role("button", name="Close").click()  # back to Loom 1's panel (diagnosis card)
            page.wait_for_timeout(400)
            row = page.locator('aside[aria-label="needs attention"] button', has_text="Loom 1")
            if not page.locator(".diag").count() and row.count():
                (
                    row.filter(has_text="Shinde").first
                    if row.filter(has_text="Shinde").count()
                    else row.first
                ).click()
            scrolled = False
            while time.monotonic() - t0 < args.max_s:
                shot()
                st = page.evaluate("fetch('/api/state').then(r => r.json()).then(s => s.demo)")
                if st.get("alerted") and not scrolled and page.locator(".diag").count():
                    page.locator(".diag").first.scroll_into_view_if_needed()  # show the diagnosis card
                    scrolled = True
                if not st.get("on") and st.get("phase", "").startswith(
                    ("bearing failed", "error", "stopped")
                ):
                    break
                page.wait_for_timeout(max(0, int(1000 / args.fps) - 150))
            page.wait_for_timeout(800)
            shot()
            page.get_by_role("button", name="Log", exact=True).click()
            page.wait_for_timeout(900)
            shot()
            browser.close()
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()
    to_gif(frames, args.out, args.width)
    print(f"{args.out}  {len(frames)} frames, {frames[-1][1]:.0f} s, {args.out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
