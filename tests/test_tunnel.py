"""scripts/tunnel.py: URL parsing, tool lookup and the terminal QR code. No tunnel is started."""

from scripts import tunnel


def test_cloudflared_url_is_found_in_its_log_line():
    line = "2026-11-20T10:00:00Z INF |  https://quiet-river-1234.trycloudflare.com                    |"
    assert tunnel.cloudflared_url(line) == "https://quiet-river-1234.trycloudflare.com"
    assert tunnel.cloudflared_url("INF Starting tunnel") is None


def test_ngrok_https_url_from_its_local_api():
    t = {"tunnels": [{"public_url": "http://a.ngrok.app"}, {"public_url": "https://a.ngrok.app"}]}
    assert tunnel.ngrok_url(t) == "https://a.ngrok.app" and tunnel.ngrok_url({"tunnels": []}) is None


def test_missing_tools_are_reported_not_guessed(monkeypatch):
    monkeypatch.setattr(tunnel.shutil, "which", lambda _n: None)
    monkeypatch.setattr(tunnel, "candidates", lambda _n: [])
    assert tunnel.find_tool() is None
    monkeypatch.setattr(tunnel.shutil, "which", lambda n: r"C:\x\ngrok.exe" if n == "ngrok" else None)
    assert tunnel.find_tool() == ("ngrok", r"C:\x\ngrok.exe")


def test_qr_code_is_printable_text():
    q = tunnel.qr_text("https://quiet-river-1234.trycloudflare.com/?view=record")
    lines = q.strip("\n").splitlines()
    assert len(lines) > 10 and all(len(x) == len(lines[0]) for x in lines)
