"""Turns a screenshot into lines of text, top to bottom.

Uses RapidOCR (runs offline, no API key, no cost). The engine loads once, on
first use, because loading takes a second or two.
"""

from __future__ import annotations

import threading

_engine = None
_lock = threading.Lock()


def _get_engine():
    global _engine
    with _lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR()
    return _engine


def read_lines(path: str) -> list[str]:
    result, _ = _get_engine()(path)
    boxes = []
    for box, text, _score in result or []:
        ys = [p[1] for p in box]
        boxes.append((min(ys), max(ys), min(p[0] for p in box), text))
    boxes.sort()
    # Boxes whose middle lies inside the previous box's height are the same line
    # (e.g. "UPI Ref No:" on the left and the number on the right).
    lines: list[list] = []
    for top, bottom, left, text in boxes:
        mid = (top + bottom) / 2
        if lines and lines[-1][0] <= mid <= lines[-1][1]:
            lines[-1][2].append((left, text))
        else:
            lines.append([top, bottom, [(left, text)]])
    return [" ".join(t for _, t in sorted(parts)) for _, _, parts in lines]
