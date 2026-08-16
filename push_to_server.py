#!/usr/bin/env python3
"""把 build_timeline.py 算出來的切點推進 coscup-time-server，讓 COSCUP Cut 匯入。

COSCUP Cut 只能從 server 匯入時間點，而且：
  - 匯入時會抓該廳「全部」時間點，依時間排序後 1/3/5 為開始、2/4/6 為結束。
  - 只要有任何一段落在目前載入的影片範圍外，整個匯出鈕就會被鎖住。
所以一次只推一天，對應你要剪的那支 OBS 檔。

用法
  # 先看會送出什麼，不會真的寫入
  python3 _timeline/push_to_server.py TR409-2 2026-08-09 --dry-run

  # 清掉該廳舊資料再推入這一天
  python3 _timeline/push_to_server.py TR409-2 2026-08-09 --replace

  # 換 server
  python3 _timeline/push_to_server.py TR409-2 2026-08-09 --server http://localhost:3000

  # 只推信心足夠的場次
  python3 _timeline/push_to_server.py TR409-2 2026-08-09 --replace --min-confidence high
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
CONFIDENCE_ORDER = {"none": 0, "low": 1, "high": 2}


def request(method: str, url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("content-type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def main() -> int:
    ap = argparse.ArgumentParser(description="推送剪輯切點到 coscup-time-server")
    ap.add_argument("room", help="廳 ID，例如 TR409-2")
    ap.add_argument("day", help="日期 YYYY-MM-DD，例如 2026-08-09")
    ap.add_argument("--server", default="http://localhost:3000", help="server 位址")
    ap.add_argument("--replace", action="store_true", help="推送前先刪掉該廳現有時間點")
    ap.add_argument("--dry-run", action="store_true", help="只印出要送什麼")
    ap.add_argument("--min-confidence", choices=["none", "low", "high"], default="none",
                    help="低於此信心的場次略過，預設全推")
    args = ap.parse_args()

    timeline = json.loads((OUT / "timeline.json").read_text(encoding="utf-8"))
    rows = timeline.get(args.room, {}).get(args.day)
    if not rows:
        print(f"找不到 {args.room} {args.day}。可用的組合：", file=sys.stderr)
        for room, days in sorted(timeline.items()):
            for day in sorted(days):
                print(f"  {room} {day}", file=sys.stderr)
        return 1

    floor = CONFIDENCE_ORDER[args.min_confidence]
    rows = [r for r in rows if CONFIDENCE_ORDER[r["confidence"]] >= floor]
    rows.sort(key=lambda r: r["start"])
    if not rows:
        print("套用信心門檻後沒有任何場次可推。", file=sys.stderr)
        return 1

    # 送進去的順序決定 start/end 角色，這裡再驗一次不會交錯
    prev_end = None
    for r in rows:
        if prev_end is not None and r["start"] <= prev_end:
            print(f"時間點重疊：{r['start']} <= 前一段結束 {prev_end}", file=sys.stderr)
            return 1
        prev_end = r["end"]

    base = args.server.rstrip("/") + "/api/v1"
    print(f"{args.room} {args.day}：{len(rows)} 段 / {len(rows) * 2} 個時間點 → {base}")
    for i, r in enumerate(rows, 1):
        mins = (dt.datetime.fromisoformat(r["end"])
                - dt.datetime.fromisoformat(r["start"])).total_seconds() / 60
        print(f"  {i:2}. {r['start'][11:19]} → {r['end'][11:19]} "
              f"({mins:5.1f}m) [{r['confidence']:4}] {r['title'][:44]}")
    if args.dry_run:
        print("\n--dry-run，沒有寫入任何東西。")
        return 0

    if args.replace:
        existing = request("GET", f"{base}/rooms/{args.room}/events") or []
        for event in existing:
            request("DELETE", f"{base}/events/{event['id']}")
        print(f"已刪除 {len(existing)} 筆舊資料")

    sent = 0
    for r in rows:
        for key in ("start", "end"):
            request("POST", f"{base}/rooms/{args.room}/events", {"recorded_at": r[key]})
            sent += 1
    print(f"已寫入 {sent} 個時間點。")
    print(f"在 COSCUP Cut 載入 {args.day} 的 OBS 檔，廳 ID 填 {args.room}，按「匯入」即可。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as err:
        print(f"連不上 server：{err}", file=sys.stderr)
        raise SystemExit(2)
