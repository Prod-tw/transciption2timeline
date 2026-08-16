#!/usr/bin/env python3
"""把每間教室每一天的 Rozeta 逐字稿合併成一個檔，給人看也給模型讀。

輸入  ../manifest.json + ../<廳>/<session>/transcriptions.json
輸出  _timeline/transcripts/<廳>_<YYYYMMDD>.txt

同一廳同一天的所有 Rozeta session 會合併去重、依 created_at 排序，
30 秒內的句子併成一行（省 token），中間的靜默直接標出來——
靜默就是換場的訊號，標成一行比讓讀者自己算時間差有用。

    python3 _timeline/build_transcripts.py
"""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

TZ = dt.timezone(dt.timedelta(hours=8))
SILENCE = 30   # 大於這個秒數就標成靜默 (秒)
MERGE = 30     # 幾秒內的句子併成一行 (秒)

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "transcripts"


def hms(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, TZ).strftime("%H:%M:%S")


def day_of(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")


def load() -> dict[str, list[tuple[int, str]]]:
    """room -> 依時間排序的 (created_at, text)，跨 Rozeta session 合併去重。"""
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    per_room: dict[str, dict[str, tuple[int, str]]] = defaultdict(dict)
    for account in manifest["accounts"]:
        room = account["account"]["room"]
        for meeting in account["meetings"]:
            rel = meeting["path"].split("completed-meetings/", 1)[-1]
            f = ROOT / rel / "transcriptions.json"
            if not f.exists():
                continue
            for item in json.loads(f.read_text(encoding="utf-8"))["items"]:
                per_room[room][item["id"]] = (item["created_at"], (item.get("text") or "").strip())
    return {room: sorted(v.values()) for room, v in per_room.items()}


def render(seq: list[tuple[int, str]]) -> tuple[str, int]:
    """壓成 `HH:MM:SS 文字` 與靜默標記，回傳 (內文, 靜默數)。"""
    lines: list[str] = []
    buf: list[str] = []
    start = prev = None
    silences = 0
    for ts, text in seq:
        if start is None:
            start = ts
        elif ts - prev >= SILENCE:
            if buf:
                lines.append(f"{hms(start)} {' '.join(buf)}")
            lines.append(f"--- 靜默 {ts - prev}s ({hms(prev)} → {hms(ts)}) ---")
            silences += 1
            buf, start = [], ts
        elif ts - start >= MERGE and buf:
            lines.append(f"{hms(start)} {' '.join(buf)}")
            buf, start = [], ts
        if text:
            buf.append(text)
        prev = ts
    if buf:
        lines.append(f"{hms(start)} {' '.join(buf)}")
    return "\n".join(lines), silences


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for stale in OUT.glob("*.txt"):
        stale.unlink()

    per_day: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    for room, seq in load().items():
        for item in seq:
            per_day[(room, day_of(item[0]))].append(item)

    total = 0
    for (room, day), seq in sorted(per_day.items()):
        body, silences = render(seq)
        header = (f"# {room} {day}  {hms(seq[0][0])}–{hms(seq[-1][0])}  "
                  f"{len(seq)} 句 / {silences} 段靜默\n\n")
        path = OUT / f"{room.replace(' ', '_')}_{day.replace('-', '')}.txt"
        path.write_text(header + body + "\n", encoding="utf-8")
        total += len(body)
        print(f"{path.name:28} {len(seq):6} 句 {len(body):8} 字 {silences:4} 靜默")

    print(f"\n{len(per_day)} 個廳-天，共 {total} 字 → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
