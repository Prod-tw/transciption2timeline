#!/usr/bin/env python3
"""把 timeline.json 廣播成一台 coscup-time-server 相容的唯讀 server。

COSCUP Cut 匯入時只會打 `GET {server}/api/v1/rooms/{廳ID}/events`，
而且廳 ID 是原封不動接進 URL path 的。所以這裡用「廳@日期」當廳 ID，
一台 server 就能同時提供全部教室、全部天數的切點，
在編輯器裡換一下廳 ID 就換一批資料，不用每次重推。

啟動
    python3 _timeline/serve_timeline.py
    python3 _timeline/serve_timeline.py --port 3000 --host 0.0.0.0
    python3 _timeline/serve_timeline.py --min-confidence high

然後打開 http://localhost:3000 會列出所有可用的廳 ID。

在 COSCUP Cut：
    Server URL  http://localhost:3000
    廳 ID       TR409-2@0809

廳 ID 可以這樣寫（大小寫、分隔符號都不挑）：
    TR409-2@0809            月日
    TR409-2@2026-08-09      完整日期
    TR409-2@2              第 2 天
    TR409-2                 只有一天有資料時可以省略
    209@0809                純數字會自動補 TR
    TR409-2@0809!high       只要信心 high 的場次

API（與 coscup-time-server 相容的唯讀子集）
    GET /api/v1/health
    GET /api/v1/rooms
    GET /api/v1/rooms/{廳ID}/events      COSCUP Cut 匯入用
    GET /api/v1/rooms/{廳ID}/sessions    附標題，給你對照用
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent
TIMELINE = HERE / "out" / "timeline.json"
TZ = dt.timezone(dt.timedelta(hours=8))
CONFIDENCE_ORDER = {"none": 0, "low": 1, "high": 2}


# --- 資料 -----------------------------------------------------------------

class Timeline:
    """載入 timeline.json，並在檔案變動時自動重載。"""

    def __init__(self, path: Path, min_confidence: str = "none"):
        self.path = path
        self.min_confidence = min_confidence
        self._lock = threading.Lock()
        self._mtime = 0.0
        self.groups: dict[tuple[str, str], list[dict]] = {}
        self.days_by_room: dict[str, list[str]] = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            mtime = self.path.stat().st_mtime
            if mtime == self._mtime:
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
            groups: dict[tuple[str, str], list[dict]] = {}
            for room, days in data.items():
                for day, rows in days.items():
                    groups[(room, day)] = list(rows)

            event_id = 1
            for key in sorted(groups):
                rows = sorted(groups[key], key=lambda r: r["start"])
                for row in rows:
                    row["_start_id"] = event_id
                    row["_end_id"] = event_id + 1
                    event_id += 2
                groups[key] = rows

            days: dict[str, list[str]] = {}
            for room, day in groups:
                days.setdefault(room, []).append(day)
            for room in days:
                days[room].sort()

            self.groups, self.days_by_room, self._mtime = groups, days, mtime

    # --- 廳 ID 解析 -------------------------------------------------------

    def resolve(self, raw: str) -> tuple[tuple[str, str] | None, str, str | None]:
        """回傳 ((room, day) 或 None, 信心門檻, 錯誤訊息)。"""
        text = unquote(raw).strip()
        if not text:
            return None, self.min_confidence, "廳 ID 不可為空"

        floor = self.min_confidence
        if "!" in text:
            text, _, level = text.rpartition("!")
            level = level.strip().lower()
            if level not in CONFIDENCE_ORDER:
                return None, floor, f"信心門檻只能是 high / low / none，收到 {level!r}"
            floor = level

        room_part, _, day_part = text.partition("@")
        room_part, day_part = room_part.strip(), day_part.strip()

        room = self._match_room(room_part)
        if room is None:
            known = "、".join(sorted(self.days_by_room))
            return None, floor, f"找不到廳 {room_part!r}。可用的廳：{known}"

        days = self.days_by_room[room]
        if not day_part:
            if len(days) == 1:
                return (room, days[0]), floor, None
            options = "、".join(f"{room}@{d[5:].replace('-', '')}" for d in days)
            return None, floor, (
                f"{room} 有 {len(days)} 天的資料，請指定日期。"
                f"一次只能匯入一天，否則 COSCUP Cut 的匯出鈕會被鎖住。可用：{options}"
            )

        day = self._match_day(days, day_part)
        if day is None:
            options = "、".join(days)
            return None, floor, f"{room} 沒有 {day_part!r} 這天。可用：{options}"
        return (room, day), floor, None

    def _match_room(self, text: str) -> str | None:
        if not text:
            return None
        rooms = self.days_by_room
        if text in rooms:
            return text
        lowered = {r.lower(): r for r in rooms}
        if text.lower() in lowered:
            return lowered[text.lower()]
        # 現場按鈕用純數字，server 端會補 TR 前綴
        if text.isdigit() and f"TR{text}" in rooms:
            return f"TR{text}"
        # 忽略空白與底線的寬鬆比對
        squashed = {re.sub(r"[\s_]+", "", r).lower(): r for r in rooms}
        key = re.sub(r"[\s_]+", "", text).lower()
        return squashed.get(key)

    @staticmethod
    def _match_day(days: list[str], text: str) -> str | None:
        text = text.lower().lstrip("d")
        digits = re.sub(r"\D", "", text)
        if text in days or digits == "":
            return text if text in days else None
        for day in days:
            compact = day.replace("-", "")
            if digits in (compact, compact[4:], day):
                return day
        if digits.isdigit() and 1 <= int(digits) <= len(days):
            return days[int(digits) - 1]
        return None

    # --- 取資料 -----------------------------------------------------------

    def rows(self, key: tuple[str, str], floor: str) -> list[dict]:
        limit = CONFIDENCE_ORDER[floor]
        return [r for r in self.groups[key] if CONFIDENCE_ORDER[r["confidence"]] >= limit]

    def events(self, key: tuple[str, str], floor: str) -> list[dict]:
        room = key[0]
        out = []
        for row in self.rows(key, floor):
            for eid, stamp, role in ((row["_start_id"], row["start"], "start"),
                                     (row["_end_id"], row["end"], "end")):
                out.append({
                    "id": eid,
                    "room_id": room,
                    "recorded_at": stamp,
                    "recorded_at_ms": int(dt.datetime.fromisoformat(stamp).timestamp() * 1000),
                    "source": "transcript",
                    "position": len(out) + 1,
                    "marker_type": role,
                })
        return out


# --- HTTP -----------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "coscup-timeline-broadcast/1.0"
    timeline: Timeline

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("  %s %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("access-control-allow-origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json")

    def error(self, status: int, message: str) -> None:
        self.json(status, {"error": message})

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        try:
            self.timeline.reload()
        except FileNotFoundError:
            self.error(500, f"找不到 {self.timeline.path}，請先跑 build_timeline.py")
            return

        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/":
            self._send(200, render_index(self.timeline).encode(), "text/html; charset=utf-8")
            return
        if path == "/api/v1/health":
            self.json(200, {"status": "ok", "rooms": len(self.timeline.days_by_room),
                            "room_days": len(self.timeline.groups),
                            "source": str(self.timeline.path)})
            return
        if path == "/api/v1/rooms":
            self.json(200, [
                {"room_id": f"{room}@{day[5:].replace('-', '')}",
                 "room": room, "day": day, "event_count": len(rows) * 2,
                 "first_event": rows[0]["start"] if rows else None,
                 "last_event": rows[-1]["end"] if rows else None}
                for (room, day), rows in sorted(self.timeline.groups.items())
            ])
            return

        match = re.fullmatch(r"/api/v1/rooms/(.+)/(events|sessions)", path)
        if not match:
            self.error(404, f"沒有這個路徑：{path}")
            return

        key, floor, err = self.timeline.resolve(match.group(1))
        if err:
            self.error(404, err)
            return

        if match.group(2) == "events":
            self.json(200, self.timeline.events(key, floor))
        else:
            self.json(200, [
                {"title": r["title"], "start": r["start"], "end": r["end"],
                 "duration_seconds": int((dt.datetime.fromisoformat(r["end"])
                                          - dt.datetime.fromisoformat(r["start"])).total_seconds()),
                 "confidence": r["confidence"]}
                for r in self.timeline.rows(key, floor)
            ])


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# --- 首頁 -----------------------------------------------------------------

CSS = """
:root { color-scheme: light dark; --bg:#fff; --fg:#1a1a1a; --muted:#666; --line:#e3e3e3;
        --chip:#f2f2f2; --ok:#0a7c3f; --warn:#9a6400; --bad:#a03030; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161616; --fg:#eaeaea; --muted:#9a9a9a; --line:#333; --chip:#242424;
          --ok:#4ec27f; --warn:#d8a13a; --bad:#e07070; } }
* { box-sizing:border-box }
body { margin:0; padding:2rem 1.5rem 4rem; background:var(--bg); color:var(--fg);
       font:15px/1.6 ui-sans-serif,-apple-system,"Noto Sans TC",sans-serif; }
main { max-width:1000px; margin:0 auto }
h1 { font-size:1.5rem; margin:0 0 .3rem }
p.lede { color:var(--muted); margin:0 0 1.5rem }
code, .id { font-family:ui-monospace,SFMono-Regular,Menlo,monospace }
.id { background:var(--chip); padding:.15rem .45rem; border-radius:5px; cursor:pointer;
      border:1px solid var(--line); font-size:.9em; white-space:nowrap }
.id:hover { border-color:var(--muted) }
.wrap { overflow-x:auto; border:1px solid var(--line); border-radius:10px }
table { border-collapse:collapse; width:100%; min-width:760px }
th, td { text-align:left; padding:.5rem .7rem; border-bottom:1px solid var(--line); font-size:.9rem }
th { font-weight:600; color:var(--muted); font-size:.78rem; text-transform:uppercase;
     letter-spacing:.04em; position:sticky; top:0; background:var(--bg) }
tbody tr:last-child td { border-bottom:none }
td.num { text-align:right; font-variant-numeric:tabular-nums }
.ok { color:var(--ok) } .warn { color:var(--warn) } .bad { color:var(--bad) }
.box { background:var(--chip); border:1px solid var(--line); border-radius:10px;
       padding:.9rem 1.1rem; margin:0 0 1.5rem }
.box p { margin:.3rem 0 }
details { margin-top:2rem } summary { cursor:pointer; color:var(--muted) }
.toast { position:fixed; left:50%; bottom:2rem; transform:translateX(-50%); background:var(--fg);
         color:var(--bg); padding:.5rem 1rem; border-radius:8px; opacity:0; transition:opacity .2s;
         pointer-events:none; font-size:.85rem }
.toast.on { opacity:1 }
"""

JS = """
document.addEventListener('click', (e) => {
  const el = e.target.closest('.id');
  if (!el) return;
  navigator.clipboard.writeText(el.textContent.trim()).then(() => {
    const t = document.querySelector('.toast');
    t.textContent = '已複製 ' + el.textContent.trim();
    t.classList.add('on');
    setTimeout(() => t.classList.remove('on'), 1400);
  });
});
"""


def render_index(timeline: Timeline) -> str:
    rows_html = []
    for (room, day), rows in sorted(timeline.groups.items()):
        counts = {"high": 0, "low": 0, "none": 0}
        for r in rows:
            counts[r["confidence"]] += 1
        if counts["none"] == len(rows):
            cls, status = "bad", "全部退回排程時間"
        elif counts["low"] or counts["none"]:
            bits = ([f"{counts['low']} 場需複查"] if counts["low"] else []) + \
                   ([f"{counts['none']} 場用排程時間"] if counts["none"] else [])
            cls, status = "warn", "、".join(bits)
        else:
            cls, status = "ok", "全部可用"
        span = ""
        if rows:
            span = f"{rows[0]['start'][11:16]}–{rows[-1]['end'][11:16]}"
        rid = f"{room}@{day[5:].replace('-', '')}"
        rows_html.append(
            f"<tr><td><span class='id'>{html.escape(rid)}</span></td>"
            f"<td>{html.escape(day)}</td><td class='num'>{len(rows)}</td>"
            f"<td class='num'>{len(rows) * 2}</td><td>{span}</td>"
            f"<td class='{cls}'>{status}</td></tr>"
        )

    total_rooms = len(timeline.days_by_room)
    total_events = sum(len(v) for v in timeline.groups.values()) * 2
    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>COSCUP 剪輯時間線廣播</title><style>{CSS}</style></head><body><main>
<h1>COSCUP 剪輯時間線</h1>
<p class="lede">{total_rooms} 間教室 / {len(timeline.groups)} 個廳-天組合 / {total_events} 個時間點，
資料來自 <code>{html.escape(str(timeline.path))}</code>，檔案更新後會自動重載。</p>

<div class="box">
  <p><strong>在 COSCUP Cut 這樣填：</strong></p>
  <p>Server URL — <code>http://{{這台機器}}:{{port}}</code></p>
  <p>廳 ID — 點下面表格的 ID 直接複製，例如 <span class="id">TR409-2@0809</span></p>
  <p style="color:var(--muted)">一個 ID 只含一天的切點。載入哪一天的 OBS 檔就用哪一天的 ID，
  不然只要有一段落在影片範圍外，編輯器的匯出鈕會整個鎖住。</p>
</div>

<div class="wrap"><table>
<thead><tr><th>廳 ID（點擊複製）</th><th>日期</th><th class="num">段數</th>
<th class="num">時間點</th><th>涵蓋時間</th><th>狀態</th></tr></thead>
<tbody>{''.join(rows_html)}</tbody></table></div>

<details><summary>廳 ID 的其他寫法與 API</summary>
<p>日期可以寫 <code>@0809</code>、<code>@2026-08-09</code>，或用第幾天 <code>@2</code>；
某間教室只有一天有資料時可以整個省略。純數字會自動補 TR 前綴（<code>209</code> → <code>TR209</code>）。
加上 <code>!high</code> 只保留信心 high 的場次，例如 <code>TR313@0808!high</code>。</p>
<p>API：<code>GET /api/v1/health</code>、<code>GET /api/v1/rooms</code>、
<code>GET /api/v1/rooms/{{廳ID}}/events</code>、
<code>GET /api/v1/rooms/{{廳ID}}/sessions</code>（附標題，給你對照用）。</p>
</details>
</main><div class="toast"></div><script>{JS}</script></body></html>"""


# --- 進入點 ---------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="把 timeline.json 廣播成 COSCUP Cut 可匯入的 server")
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"),
                    help="預設 0.0.0.0，同網段的機器也能連（env HOST）")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "3000")),
                    help="env PORT")
    ap.add_argument("--timeline", type=Path,
                    default=Path(os.environ.get("TIMELINE_PATH", TIMELINE)),
                    help="timeline.json 路徑（env TIMELINE_PATH）")
    ap.add_argument("--min-confidence", choices=["none", "low", "high"],
                    default=os.environ.get("MIN_CONFIDENCE", "none"),
                    help="全域信心門檻，預設全給；廳 ID 加 !high 可個別覆寫（env MIN_CONFIDENCE）")
    args = ap.parse_args()

    if not args.timeline.exists():
        print(f"找不到 {args.timeline}，請先執行： python3 _timeline/build_timeline.py", file=sys.stderr)
        return 1

    timeline = Timeline(args.timeline, args.min_confidence)
    Handler.timeline = timeline

    shown = "localhost" if args.host in ("0.0.0.0", "") else args.host
    print(f"COSCUP 剪輯時間線廣播中 → http://{shown}:{args.port}")
    print(f"  {len(timeline.days_by_room)} 間教室 / {len(timeline.groups)} 個廳-天組合")
    print(f"  資料來源 {args.timeline}（更新後自動重載）")
    if args.min_confidence != "none":
        print(f"  全域信心門檻 {args.min_confidence}")
    print("  在 COSCUP Cut 填 Server URL 與廳 ID，例如 TR409-2@0809")
    try:
        with Server((args.host, args.port), Handler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
