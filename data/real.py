"""Download the real datasets Dhadkan is evaluated on. Resumable; skips what is already there.

    python -m data.real [dcase2020] [ims] [cwru]        # default: all three

DCASE 2020 Task 2 development set (pump, fan, valve, slider): official train (normal only) and
  test (normal + anomalous) splits, Zenodo record 3678171, CC BY 4.0.
NASA IMS bearings (Lee, Qiu, Yu, Lin, Rexnord / U. Cincinnati), run-to-failure, via the NASA
  Prognostics Center of Excellence data repository. Only the bearing channels that failed (plus
  test 2's healthy bearings, for false-alarm checks) are kept, as float16 arrays + timestamps.
CWRU Bearing Data Center (Case Western Reserve University): 12 kHz drive-end fault files and the
  normal baseline. The fault label of every file is parsed from the link text on CWRU's own pages.
"""

import io
import json
import re
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
LOG = RAW / "download_real.log"
UA = {"User-Agent": "dhadkan-data-downloader"}

DCASE_RECORD = "https://zenodo.org/records/3678171/files/{name}?download=1"
DCASE_TYPES = ("pump", "fan", "valve", "slider")
DCASE_TRAIN_PER_ID = 100  # of ~900-1,000 per machine ID; the whole test split is always downloaded
IMS_URL = "https://phm-datasets.s3.amazonaws.com/NASA/4.+Bearings.zip"
# Which channel is which bearing, and which bearing failed how, per the IMS readme (checked in RESULTS.md).
IMS_KEEP = {
    "1st_test": {"b3": 4, "b4": 6},
    "2nd_test": {"b1": 0, "b2": 1, "b3": 2, "b4": 3},
    "3rd_test": {"b3": 2},
}
CWRU_PAGES = {
    "fault_12k_de": "https://engineering.case.edu/bearingdatacenter/12k-drive-end-bearing-fault-data",
    "normal": "https://engineering.case.edu/bearingdatacenter/normal-baseline-data",
}


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    RAW.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def fetch(url: str, out: Path, tries: int = 8) -> Path:
    """Stream `url` to `out`, resuming a partial file; retries with backoff."""
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(out.suffix + ".part")
    for attempt in range(tries):
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(url, headers={**UA, **({"Range": f"bytes={have}-"} if have else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r, part.open("ab" if have else "wb") as f:
                total = have + int(r.headers.get("Content-Length", 0))
                last = time.time()
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    if time.time() - last > 30:
                        log(f"  {out.name}: {f.tell() / 1e9:.2f} / {total / 1e9:.2f} GB")
                        last = time.time()
            part.rename(out)
            return out
        except Exception as e:  # network hiccup or rate limit: wait and resume
            wait = min(300, 10 * 2**attempt)
            log(f"  {out.name}: {e!r}; retry in {wait}s")
            time.sleep(wait)
    raise SystemExit(f"could not download {url}")


def fetch_parallel(url: str, out: Path, parts: int = 8) -> Path:
    """Large files: download byte ranges on several connections at once (each part resumes)."""
    from concurrent.futures import ThreadPoolExecutor

    if out.exists():
        return out
    head = urllib.request.urlopen(urllib.request.Request(url, headers=UA, method="HEAD"), timeout=60)
    size = int(head.headers.get("Content-Length", 0))
    if not size or head.headers.get("Accept-Ranges") != "bytes":
        return fetch(url, out)
    edges = [size * i // parts for i in range(parts + 1)]

    def part(i: int) -> Path:
        p = out.with_suffix(out.suffix + f".p{i}")
        lo, hi = edges[i], edges[i + 1] - 1
        for attempt in range(10):
            have = p.stat().st_size if p.exists() else 0
            if have >= hi - lo + 1:
                return p
            req = urllib.request.Request(url, headers={**UA, "Range": f"bytes={lo + have}-{hi}"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r, p.open("ab") as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
            except Exception as e:
                wait = min(300, 10 * 2**attempt)
                log(f"  {out.name} part {i}: {e!r}; retry in {wait}s")
                time.sleep(wait)
        raise SystemExit(f"could not download part {i} of {url}")

    out.parent.mkdir(parents=True, exist_ok=True)
    log(f"  {out.name}: {size / 1e9:.2f} GB on {parts} connections")
    with ThreadPoolExecutor(parts) as ex:
        files = list(ex.map(part, range(parts)))
    with out.with_suffix(out.suffix + ".part").open("wb") as f:
        for p in files:
            f.write(p.read_bytes())
            p.unlink()
    out.with_suffix(out.suffix + ".part").rename(out)
    return out


def dcase2020(train_per_id: int = DCASE_TRAIN_PER_ID) -> None:
    """The full official test split, and the first `train_per_id` normal training clips of each machine ID.

    Clips are read straight out of the Zenodo zips with HTTP range requests (data/download.py), so the
    rest of each 1 GB archive is never downloaded.
    """
    from concurrent.futures import ThreadPoolExecutor

    from .download import _zip
    from .download import fetch as fetch_member

    for mtype in DCASE_TYPES:
        url = DCASE_RECORD.format(name=f"dev_data_{mtype}.zip")
        names = [n for n in _zip(url).namelist() if n.endswith(".wav")]
        test = sorted(n for n in names if re.match(rf"{mtype}/test/", n))
        by_id: dict[str, list[str]] = {}
        for n in sorted(n for n in names if re.match(rf"{mtype}/train/normal_id_\d\d_", n)):
            by_id.setdefault(re.search(r"id_(\d\d)", n).group(1), []).append(n)  # type: ignore[union-attr]
        train = [n for ids in by_id.values() for n in ids[:train_per_id]]
        log(
            f"dcase2020 {mtype}: {len(test)} test + {len(train)} train clips (of {sum(map(len, by_id.values()))})"
        )
        out = RAW / "dcase2020"
        with ThreadPoolExecutor(4) as ex:
            done = list(ex.map(lambda m, u=url, o=out: fetch_member(u, m, o), test + train))
        log(f"dcase2020 {mtype}: {done.count('ok')} downloaded, {done.count('skip')} already there")
        meta = {"record": "https://zenodo.org/records/3678171", "test": len(test), "train_used": len(train),
                "train_available": {k: len(v) for k, v in by_id.items()}}  # fmt: skip
        (out / mtype / "subset.json").write_text(json.dumps(meta, indent=1))


def ims() -> None:
    dest = RAW / "ims"
    if (dest / "meta.json").exists():
        log("ims: already prepared")
        return
    z = fetch_parallel(IMS_URL, dest / "bearings.zip", 8)
    work = dest / "work"
    work.mkdir(parents=True, exist_ok=True)
    log("ims: unpacking (zip -> 7z -> rar)")
    with zipfile.ZipFile(z) as zf:
        zf.extractall(work)
    for archive in sorted(work.rglob("*.7z")):
        subprocess.run(["bsdtar", "-xf", str(archive), "-C", str(work)], check=True)
    meta: dict = {"source": IMS_URL, "tests": {}}
    for test, chans in IMS_KEEP.items():
        rar = next(work.rglob(f"{test}.rar"), None)
        own = work / f"x_{test}"  # each test unpacks into its own folder, whatever it is called inside
        own.mkdir(exist_ok=True)
        if rar:
            subprocess.run(["bsdtar", "-xf", str(rar), "-C", str(own)], check=True)
        stamp = re.compile(r"\d{4}\.\d\d\.\d\d\.\d\d\.\d\d\.\d\d$")
        files = sorted(
            (f for f in own.rglob("*") if f.is_file() and stamp.match(f.name)), key=lambda f: f.name
        )
        if not files:
            log(f"ims {test}: no snapshot files found")
            continue
        log(f"ims {test}: {len(files)} snapshots, keeping {list(chans)}")
        arrs = {b: [] for b in chans}
        for f in files:
            x = np.loadtxt(f, dtype=np.float32)
            for b, c in chans.items():
                arrs[b].append(x[:, c].astype(np.float16))
        stamps = [time.mktime(time.strptime(f.name, "%Y.%m.%d.%H.%M.%S")) for f in files]
        np.save(dest / f"{test}_t.npy", np.array(stamps))
        for b, a in arrs.items():
            np.save(dest / f"{test}_{b}.npy", np.stack(a))
        meta["tests"][test] = {
            "snapshots": len(files),
            "first": files[0].name,
            "last": files[-1].name,
            "bearings": list(chans),
        }
    readme = next(work.rglob("*.pdf"), None)
    if readme:
        readme.replace(dest / "IMS_readme.pdf")
    (dest / "meta.json").write_text(json.dumps(meta, indent=1))
    subprocess.run(["rm", "-rf", str(work)], check=True)
    z.unlink()
    log("ims: done")


def cwru() -> None:
    dest = RAW / "cwru"
    dest.mkdir(parents=True, exist_ok=True)
    index = []
    for url in CWRU_PAGES.values():
        html = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read().decode()
        for href, label in re.findall(r'href="([^"]+\.mat)"[^>]*>([^<]*)<', html):
            label = label.strip()
            # keep normal, inner race, ball and outer race centred @6:00 (the load zone)
            m = re.match(r"(Normal|IR|B|OR)(\d{3})?(@6)?_(\d)$", label)
            if not m or (m.group(1) == "OR" and m.group(3) != "@6"):
                continue
            kind = {"Normal": "normal", "IR": "inner_race", "B": "ball", "OR": "outer_race"}[m.group(1)]
            index.append({"file": href.rsplit("/", 1)[1], "url": href, "label": label, "fault": kind,
                          "size_in": None if kind == "normal" else int(m.group(2)) / 1000,
                          "load_hp": int(m.group(4)), "page": url})  # fmt: skip
    for row in index:
        out = dest / row["file"]
        if not out.exists():
            fetch(row["url"], out)
    (dest / "index.json").write_text(json.dumps(index, indent=1))
    log(f"cwru: {len(index)} files ({sum(r['fault'] != 'normal' for r in index)} faults)")


def main() -> None:
    which = sys.argv[1:] or ["dcase2020", "ims", "cwru"]
    for name in which:
        {"dcase2020": dcase2020, "ims": ims, "cwru": cwru}[name]()


if __name__ == "__main__":
    io.DEFAULT_BUFFER_SIZE = 1 << 20
    main()
