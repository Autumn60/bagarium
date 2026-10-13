"""Human-readable formatting helpers."""

from __future__ import annotations

from datetime import datetime


def time(ns: int | None) -> str:
    if ns is None:
        return "-"
    return datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d %H:%M:%S")


def duration(start_ns: int | None, end_ns: int | None) -> str:
    if start_ns is None or end_ns is None:
        return "-"
    seconds = (end_ns - start_ns) / 1e9
    if seconds < 60:
        return f"{seconds:.1f}s"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def size(n: int | None) -> str:
    if n is None:
        return "-"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{n}B"


def count(n: int | None) -> str:
    return "-" if n is None else f"{n:,}"
