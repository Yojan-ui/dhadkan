"""HTTPS for the phone's microphone: a tunnel to the backend, with the recorder's URL as a QR code.

    python -m scripts.tunnel [--port 8600]            (.\\dev.ps1 tunnel  /  make tunnel)

A phone browser only gives a web page the microphone over HTTPS (or on localhost). This starts
cloudflared ("quick tunnel", no account) or, if that is not installed, ngrok, pointed at the backend on
--port, and prints https://<random>/?view=record plus a QR code to scan. Ctrl-C closes the tunnel.

On the laptop itself no tunnel is needed: http://localhost:8600/?view=record is a secure context.

Install one of them once (Windows):  winget install --id Cloudflare.cloudflared
                                     winget install --id Ngrok.Ngrok   (then: ngrok config add-authtoken ...)
Anyone with the URL reaches the backend while the tunnel is open: close it after the demo.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CLOUDFLARE_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
INSTALL = {
    "cloudflared": "winget install --id Cloudflare.cloudflared",
    "ngrok": "winget install --id Ngrok.Ngrok   (then: ngrok config add-authtoken <your token>)",
}


def candidates(name: str) -> list[Path]:
    """Where Windows installers put the tool, besides PATH."""
    env = os.environ
    exe = f"{name}.exe"
    dirs = [env.get("LOCALAPPDATA", "") + r"\Microsoft\WinGet\Links", env.get("ProgramFiles", "") + rf"\{name}",
            env.get("ProgramFiles(x86)", "") + rf"\{name}", env.get("USERPROFILE", "") + rf"\{name}"]  # fmt: skip
    return [Path(d) / exe for d in dirs if d.strip("\\")]


def find_tool(names: tuple[str, ...] = ("cloudflared", "ngrok")) -> tuple[str, str] | None:
    for name in names:
        hit = shutil.which(name) or next((str(p) for p in candidates(name) if p.exists()), None)
        if hit:
            return name, hit
    return None


def cloudflared_url(line: str) -> str | None:
    m = CLOUDFLARE_URL.search(line)
    return m.group(0) if m else None


def ngrok_url(tunnels: dict) -> str | None:
    for t in tunnels.get("tunnels", []):
        if str(t.get("public_url", "")).startswith("https://"):
            return t["public_url"]
    return None


def qr_text(url: str) -> str:
    """The QR code as text blocks for the terminal (segno)."""
    import io

    import segno

    buf = io.StringIO()
    segno.make(url, error="m").terminal(out=buf, compact=True, border=2)
    return buf.getvalue()


def backend_up(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


def announce(url: str, port: int) -> None:
    rec = url.rstrip("/") + "/?view=record"
    print("\n" + "=" * 64)
    print(f"  Recorder on the phone (HTTPS):  {rec}")
    print(f"  On this laptop, no tunnel:      http://localhost:{port}/?view=record")
    print("=" * 64)
    print(qr_text(rec))
    print(
        "Scan it with the phone's camera. Ctrl-C closes the tunnel (anyone with the URL can reach the backend)."
    )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):  # QR blocks and ₹ on the Windows console
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m scripts.tunnel")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8600)))
    a = ap.parse_args()
    tool = find_tool()
    if tool is None:
        print("Neither cloudflared nor ngrok is installed. Install one (Windows):")
        for name, cmd in INSTALL.items():
            print(f"  {name}: {cmd}")
        print(f"On this laptop no tunnel is needed: http://localhost:{a.port}/?view=record")
        return 2
    if not backend_up(a.port):
        print(
            f"note: nothing answers on http://127.0.0.1:{a.port} yet; start the backend (.\\dev.ps1 demo-ims or serve)"
        )
    name, exe = tool
    print(f"starting {name} ({exe}) -> http://localhost:{a.port}")
    if name == "cloudflared":
        cmd = [exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{a.port}"]
    else:
        cmd = [exe, "http", str(a.port), "--log", "stdout"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace")  # fmt: skip
    url = None
    t0 = time.time()
    try:
        assert proc.stdout is not None
        while url is None and time.time() - t0 < 60:
            if name == "cloudflared":
                line = proc.stdout.readline()
                if not line and proc.poll() is not None:
                    break
                url = cloudflared_url(line)
            else:
                time.sleep(1)
                try:
                    with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as r:
                        url = ngrok_url(json.loads(r.read().decode("utf-8")))
                except OSError:
                    pass
        if url is None:
            print(f"{name} did not give a URL within 60 s (exit code {proc.poll()}).")
            proc.terminate()
            return 1
        announce(url, a.port)
        for _ in proc.stdout:  # keep reading so the tool never blocks on a full pipe
            pass
        return proc.wait()
    except KeyboardInterrupt:
        print("\nclosing the tunnel")
        proc.terminate()
        return 0


if __name__ == "__main__":
    sys.exit(main())
