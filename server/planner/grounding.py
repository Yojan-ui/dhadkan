"""Grounding: every number, ₹ amount and part number in an LLM reply must come from that run's tool results.

    g = check(reply, tool_results, extra=[owner_text, "Loom 1", ...])
    g["ok"] -> False: the reply is rejected, the planner sends the rules reply and logs the rejection.

What counts as "from the tool results":
  * the same number (Indian-script digits are read as digits; 1,56,960 and 156960 are the same);
  * a rounding of one (₹1,57,000 for 156960 is NOT accepted: rounding to a coarser figure is a new number;
    rounding a decimal to fewer places is: 0.423 -> 0.42, 42.3 -> 42);
  * a probability shown as a percentage (0.42 -> 42%), or rupees shown in lakh/crore (156960 -> 1.57 lakh);
  * the numbers in `extra`: the owner's own message, machine names (Loom 1), and the reply options (1, 2, 3).
Part numbers (6205-2RS, B-42, UCP 206) must appear in the tool results as written (case-insensitive).
"""

import json
import re
from typing import Any

# Devanagari, Bengali, Gurmukhi, Gujarati, Oriya, Tamil, Telugu, Kannada, Malayalam digits -> 0-9
_DIGITS = {
    chr(base + i): str(i)
    for base in (0x966, 0x9E6, 0xA66, 0xAE6, 0xB66, 0xBE6, 0xC66, 0xCE6, 0xD66)
    for i in range(10)
}
_TR = str.maketrans(_DIGITS)
NUM = re.compile(r"(?<![\w.])\d+(?:,\d+)*(?:\.\d+)?")
PART = re.compile(
    r"\b(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b|\b[A-Z]{2,5} ?\d{3,}\b"
)
ALWAYS = {"1", "2", "3"}  # the numbered reply options every question uses


def norm(text: str) -> str:
    return text.translate(_TR)


def numbers(text: str) -> list[str]:
    """Numbers as written, canonical: Indian digits to Latin, thousands separators removed."""
    return [m.group(0).replace(",", "") for m in NUM.finditer(norm(text))]


def parts(text: str) -> list[str]:
    return [m.group(0) for m in PART.finditer(norm(text))]


def _flat(sources: list[Any]) -> str:
    return "\n".join(
        s if isinstance(s, str) else json.dumps(s, ensure_ascii=False, default=str) for s in sources
    )


def allowed_numbers(sources: list[Any]) -> set[str]:
    """Every number in the sources, plus the forms it may be shown in (percent, lakh/crore, rounded)."""
    out: set[str] = set()
    for raw in numbers(_flat(sources)):
        v = float(raw)
        out.add(raw)
        forms = [v, round(v), round(v, 1), round(v, 2)]
        if 0 <= v <= 1:
            forms += [round(v * 100), round(v * 100, 1)]  # probability -> %
        if v >= 1e5:
            forms += [round(v / 1e5, 2), round(v / 1e5, 1)]  # lakh
        if v >= 1e7:
            forms += [round(v / 1e7, 2), round(v / 1e7, 1)]  # crore
        for f in forms:
            out.add(_canon(f))
    return out


def _canon(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.10g}"


def check(reply: str, sources: list[Any], extra: list[str] | None = None) -> dict[str, Any]:
    """{ok, unknown_numbers, unknown_parts}: ok only when nothing in the reply is unaccounted for."""
    allowed = allowed_numbers(sources) | ALWAYS
    for e in extra or []:
        allowed |= {_canon(float(n)) for n in numbers(e)} | set(numbers(e))
    text = _flat(sources).lower() + "\n" + "\n".join(extra or []).lower()
    unknown_n = [n for n in numbers(reply) if n not in allowed and _canon(float(n)) not in allowed]
    unknown_p = [p for p in parts(reply) if p.lower() not in text and p.lower().replace(" ", "") not in text]
    return {"ok": not unknown_n and not unknown_p, "unknown_numbers": unknown_n, "unknown_parts": unknown_p}
