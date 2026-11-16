"""Keys come from the environment or a git-ignored .env file, never from code."""

import os
from functools import cache

from edge.config import ROOT


@cache
def _dotenv() -> dict[str, str]:
    path = ROOT / ".env"
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def get(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key) or _dotenv().get(key) or default
