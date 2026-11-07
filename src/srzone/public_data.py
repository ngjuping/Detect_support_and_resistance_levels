# Licensed under GNU GPL v3.0, same as this project.
"""Public historical files from https://data.binance.vision.

``GET https://fapi.binance.com`` is a different host. This module does not
send API keys. Checksums are the ``.CHECKSUM`` sidecar published next to
each zip (sha256).
"""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from pathlib import Path
from urllib.request import urlopen

from .models import Bar

VISION = "https://data.binance.vision/data/futures/um/monthly"


class PublicDataError(RuntimeError):
    pass


def kline_url(symbol: str, interval: str, year: int, month: int) -> str:
    stamp = f"{year:04d}-{month:02d}"
    name = f"{symbol}-{interval}-{stamp}.zip"
    return f"{VISION}/klines/{symbol}/{interval}/{name}"


def funding_url(symbol: str, year: int, month: int) -> str:
    stamp = f"{year:04d}-{month:02d}"
    name = f"{symbol}-fundingRate-{stamp}.zip"
    return f"{VISION}/fundingRate/{symbol}/{name}"


def download_verified(url: str, cache_dir: Path) -> bytes:
    cache_dir.mkdir(parents=True, exist_ok=True)
    name = url.rsplit("/", 1)[-1]
    dest = cache_dir / name
    if not dest.exists():
        _download(url, dest)
    payload = dest.read_bytes()
    checksum_url = url + ".CHECKSUM"
    expected = _read_checksum(_download_text(checksum_url))
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise PublicDataError(f"checksum mismatch for {name}: {actual} != {expected}")
    return payload


def parse_kline_zip(payload: bytes) -> list[Bar]:
    text = _zip_text(payload)
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    start = 1 if rows[0] and rows[0][0] == "open_time" else 0
    bars = []
    for row in rows[start:]:
        if len(row) < 6:
            continue
        bars.append(Bar(
            open_time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            close_time=int(row[6]) if len(row) > 6 and row[6] else int(row[0]),
        ))
    bars.sort(key=lambda b: b.open_time)
    dedup = {}
    for bar in bars:
        dedup[bar.open_time] = bar  # last copy wins
    return [dedup[k] for k in sorted(dedup)]


def parse_funding_zip(payload: bytes) -> list[tuple[int, float]]:
    text = _zip_text(payload)
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    start = 1 if rows[0] and rows[0][0] == "calc_time" else 0
    # Header from Vision: calc_time,funding_interval_hours,last_funding_rate
    out = []
    for row in rows[start:]:
        if len(row) < 3:
            continue
        out.append((int(row[0]), float(row[2])))
    return out


def funding_holes(
    points: list[tuple[int, float]],
    step_ms: int = 8 * 3_600_000,
    tolerance_ms: int = 60_000,
) -> list[int]:
    """Missing funding stamps. A 1ms timestamp jitter is not a hole."""

    if not points:
        return []
    times = sorted({t for t, _ in points})
    missing = []
    for prev, nxt in zip(times, times[1:]):
        if nxt - prev <= step_ms + tolerance_ms:
            continue
        cursor = prev + step_ms
        while cursor < nxt - tolerance_ms:
            missing.append(cursor)
            cursor += step_ms
    return missing


def _zip_text(payload: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if not names:
            raise PublicDataError("zip has no csv")
        return zf.read(names[0]).decode("utf-8")


def _download(url: str, dest: Path) -> None:
    with urlopen(url, timeout=60) as resp:  # noqa: S310 - public historical file
        dest.write_bytes(resp.read())


def _download_text(url: str) -> str:
    with urlopen(url, timeout=60) as resp:  # noqa: S310
        return resp.read().decode("utf-8")


def _read_checksum(text: str) -> str:
    token = text.strip().split()[0]
    if len(token) != 64:
        raise PublicDataError("checksum file did not contain sha256")
    return token
