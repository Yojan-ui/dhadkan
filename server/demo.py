"""`make demo`: serve the API and the built web app on one port, start the replay, open the browser.

python -m server.demo [--port 8600] [--no-replay] [--no-open]
"""

import argparse
import json
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from .app import create_app


def _kick(url: str, replay: bool, open_browser: bool) -> None:
    """Wait for the server, then start the demo replay and open the page."""
    for _ in range(120):
        try:
            urllib.request.urlopen(f"{url}/api/meta", timeout=1)
            break
        except OSError:
            time.sleep(0.25)
    if replay:
        req = urllib.request.Request(
            f"{url}/api/replay", data=json.dumps({"on": True}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )  # fmt: skip
        urllib.request.urlopen(req, timeout=5)
    print(f"Dhadkan is running at {url}  (Ctrl-C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8600)
    ap.add_argument("--no-replay", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    threading.Thread(target=_kick, args=(url, not args.no_replay, not args.no_open), daemon=True).start()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
