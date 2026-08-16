#!/usr/bin/env python3
"""把「當天議程表 + 該廳一整天的逐字稿」一次餵給模型，由模型判斷每場議程真正的起訖時間。

輸入
  - _timeline/opass.json              官方議程 (https://coscup.org/2026/api/opass.json)
  - _timeline/transcripts/<廳>_<YYYYMMDD>.txt   build_transcripts.py 的產物

輸出
  - _timeline/out/raw/<廳>_<YYYYMMDD>.json      模型原始回答（快取，重跑時跳過）
  - _timeline/out/timeline.json                 廳 → 日期 → [{start, end, title, confidence}]

用法
    python3 _timeline/build_timeline.py --api-key <gcli2api 的 PASSWORD>
    python3 _timeline/build_timeline.py --room TR409-2
    python3 _timeline/build_timeline.py --force        # 忽略快取重跑
    python3 _timeline/build_timeline.py --dry-run      # 只印 prompt 大小，不呼叫 API

沒有逐字稿的廳-天不呼叫模型，直接用排程時間、confidence = none。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

TZ = dt.timezone(dt.timedelta(hours=8))
HERE = Path(__file__).resolve().parent
TRANSCRIPTS = HERE / "transcripts"
OUT = HERE / "out"
RAW = OUT / "raw"

DRIFT_WARN = 30 * 60   # 與排程差超過這麼久就降 low (秒)
RETRY_WAITS = (10, 30, 60)

SYSTEM_PROMPT = """\
你是影片剪輯助理。使用者會給你某間教室某一天的議程表，以及那間教室整天的錄音逐字稿。
你的工作是判斷每一場議程「實際」的開始與結束時間，讓剪輯師照著切片。

逐字稿格式：每行是 `HH:MM:SS 該時段的語音內容`，中間穿插 `--- 靜默 N s (HH:MM:SS → HH:MM:SS) ---`。

判斷準則：
- 時間戳是那一段語音「結束」的時間，所以議程起點請往前抓一點。
- 教室麥克風整天開著。開場前工作人員測麥（「123、測試」）、閒聊、搬東西都不算議程內容。
- 換場訊號：長靜默、結束語（謝謝大家、有沒有問題、我的分享到這裡）、
  開場語（掌聲歡迎、下一位講者、大家好、接下來）。
- 切點寧可偏早也不要偏晚：偏早只是多剪到休息時間，偏晚會吃掉講者的開場。
- 排程時間只是參考，實際常常提前或延後，但通常不會差超過 30 分鐘。

每一場都要回答，用議程表的編號 index 對回去，不要重打標題。confidence 的意思：
- "high"：逐字稿裡找得到明確的起訖訊號。
- "low"：逐字稿有覆蓋但訊號模糊，或這一段錄音頭尾不完整。
- "none"：這場完全找不到對應語音（整段靜默或根本沒錄到），start/end 留空字串。

只輸出 JSON，不要任何說明文字：
{"sessions": [{"index": 1, "start": "HH:MM:SS", "end": "HH:MM:SS", "confidence": "high"}]}
"""


# --- 載入 -----------------------------------------------------------------

def load_opass(path: Path) -> dict[tuple[str, str], list[dict]]:
    """(廳, 日期) -> 依時間排序的議程。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    rooms = {r["id"]: r["zh"]["name"] for r in data["rooms"]}
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in data["sessions"]:
        start = dt.datetime.fromisoformat(s["start"]).astimezone(TZ)
        end = dt.datetime.fromisoformat(s["end"]).astimezone(TZ)
        room = rooms.get(s["room"], s["room"])
        grouped[(room, start.strftime("%Y-%m-%d"))].append({
            "start": start, "end": end, "title": s["zh"]["title"].strip(),
        })
    return {k: sorted(v, key=lambda s: (s["start"], s["end"])) for k, v in grouped.items()}


def transcript_path(room: str, day: str) -> Path:
    return TRANSCRIPTS / f"{room.replace(' ', '_')}_{day.replace('-', '')}.txt"


def coverage(text: str) -> tuple[str, str] | None:
    """從檔頭 `# TR411 2026-08-09  08:22:34–16:06:01 ...` 取逐字稿涵蓋範圍。"""
    m = re.search(r"(\d\d:\d\d:\d\d)[–-](\d\d:\d\d:\d\d)", text.split("\n", 1)[0])
    return (m.group(1), m.group(2)) if m else None


# --- Prompt ---------------------------------------------------------------

def build_prompt(room: str, day: str, sessions: list[dict], transcript: str) -> str:
    agenda = "\n".join(
        f"{i}. {s['start']:%H:%M}–{s['end']:%H:%M}  {s['title']}"
        for i, s in enumerate(sessions, 1)
    )
    return (f"【教室】{room}　【日期】{day}\n\n"
            f"【議程表】共 {len(sessions)} 場（排程時間）\n{agenda}\n\n"
            f"【逐字稿】\n{transcript}")


# --- API ------------------------------------------------------------------

def call_api(url: str, key: str, model: str, prompt: str, timeout: int) -> str:
    body = json.dumps({
        "model": model,
        "stream": False,
        "temperature": 0,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(f"{url.rstrip('/')}/chat/completions", data=body, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("authorization", f"Bearer {key}")

    for attempt, wait in enumerate((*RETRY_WAITS, None)):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            usage = data.get("usage") or {}
            if usage:
                print(f"      tokens: prompt {usage.get('prompt_tokens')} "
                      f"/ completion {usage.get('completion_tokens')}")
            return data["choices"][0]["message"]["content"]
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as err:
            code = getattr(err, "code", None)
            if wait is None or (code is not None and code not in (408, 429, 500, 502, 503, 504)):
                raise
            print(f"      第 {attempt + 1} 次失敗（{err}），{wait}s 後重試")
            time.sleep(wait)
    raise RuntimeError("unreachable")


def parse_answer(text: str) -> list[dict]:
    """容忍 ```json 圍籬與前後雜訊，抓出最外層的 JSON 物件。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"回應裡沒有 JSON：{text[:200]!r}")
    return json.loads(m.group(0))["sessions"]


# --- 驗證 -----------------------------------------------------------------

def to_dt(day: str, hms: str) -> dt.datetime:
    return dt.datetime.strptime(f"{day} {hms}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)


def validate(day: str, sessions: list[dict], answer: list[dict],
             cover: tuple[str, str] | None) -> list[dict]:
    """把模型回答對回議程；不合理的退回排程時間。整批對不上就 raise。"""
    by_index = {a.get("index"): a for a in answer}
    if sorted(by_index) != list(range(1, len(sessions) + 1)):
        raise ValueError(f"index 對不上：期望 1..{len(sessions)}，收到 {sorted(by_index)}")

    lo = to_dt(day, cover[0]) if cover else None
    hi = to_dt(day, cover[1]) if cover else None

    rows = []
    for i, s in enumerate(sessions, 1):
        a = by_index[i]
        conf = a.get("confidence", "none")
        start = end = None
        try:
            if conf != "none" and a.get("start") and a.get("end"):
                start, end = to_dt(day, a["start"]), to_dt(day, a["end"])
                if start >= end:
                    raise ValueError("start >= end")
                if lo and not (lo <= start and end <= hi):
                    raise ValueError("超出逐字稿涵蓋範圍")
        except ValueError as err:
            print(f"      第 {i} 場退回排程時間：{err}")
            start = end = None
        if start is None:
            start, end, conf = s["start"], s["end"], "none"
        elif conf not in ("high", "low"):
            conf = "low"
        elif (abs((start - s["start"]).total_seconds()) > DRIFT_WARN
              or abs((end - s["end"]).total_seconds()) > DRIFT_WARN):
            conf = "low"
        rows.append({"start": start, "end": end, "title": s["title"], "confidence": conf})

    def revert(i: int) -> None:
        rows[i].update(start=sessions[i]["start"], end=sessions[i]["end"], confidence="none")

    # 議程是照時間排的，所以模型給的時間段也必須遞增。實測模型偶爾會把幾場的時間串錯位
    # （index 4 拿到第 5 場的時間，甚至三場輪轉），時間段本身是對的、只是配錯人。
    # 依時間排序重新對應回議程順序，被改到的降 low 讓人複查。
    timed = [i for i, r in enumerate(rows) if r["confidence"] != "none"]
    spans = [(rows[i]["start"], rows[i]["end"]) for i in timed]
    if spans != sorted(spans):
        print("      時間順序與議程不一致，依時間重新對應（被改到的降 low）")
        for i, span in zip(timed, sorted(spans)):
            if (rows[i]["start"], rows[i]["end"]) != span:
                rows[i]["start"], rows[i]["end"] = span
                rows[i]["confidence"] = "low"

    # 剩下的小幅重疊夾到中點
    for a, b in zip(rows, rows[1:]):
        if a["end"] > b["start"]:
            mid = a["end"] + (b["start"] - a["end"]) / 2
            if not (a["start"] < mid < b["end"]):
                continue                      # 夾了會變成負長度，留給下面的保險
            a["end"] = b["start"] = mid
            for r in (a, b):
                if r["confidence"] == "high":
                    r["confidence"] = "low"

    # 保險：走到這裡還是負長度的，一律退回排程時間
    for i, r in enumerate(rows):
        if r["start"] >= r["end"]:
            print(f"      第 {i + 1} 場長度異常，退回排程時間")
            revert(i)
    return rows


# --- 主流程 ---------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="用模型從逐字稿推導每場議程的實際起訖時間")
    ap.add_argument("--api-url", default=os.environ.get("API_URL", "http://192.168.1.231:7861/antigravity/v1"))
    ap.add_argument("--api-key", default=os.environ.get("API_KEY", ""),
                    help="gcli2api 的 PASSWORD（env API_KEY）")
    ap.add_argument("--model", default=os.environ.get("MODEL", "gemini-3.7-flash-medium"))
    ap.add_argument("--room", help="只跑這一間教室")
    ap.add_argument("--force", action="store_true", help="忽略 out/raw/ 的快取重跑")
    ap.add_argument("--dry-run", action="store_true", help="只印 prompt 大小，不呼叫 API")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--opass", type=Path, default=HERE / "opass.json")
    args = ap.parse_args()

    if not args.opass.exists():
        print(f"找不到議程檔 {args.opass}\n下載： "
              "curl -sL https://coscup.org/2026/api/opass.json -o _timeline/opass.json",
              file=sys.stderr)
        return 1
    if not args.api_key and not args.dry_run:
        print("需要 --api-key（或環境變數 API_KEY），就是 gcli2api 的 PASSWORD", file=sys.stderr)
        return 1

    grouped = load_opass(args.opass)
    keys = sorted(k for k in grouped if not args.room or k[0] == args.room)
    if not keys:
        print(f"找不到教室 {args.room!r}。可用：{'、'.join(sorted({k[0] for k in grouped}))}",
              file=sys.stderr)
        return 1

    RAW.mkdir(parents=True, exist_ok=True)
    # --room 只更新那一廳，其他廳沿用現有結果
    existing = OUT / "timeline.json"
    timeline: dict[str, dict[str, list[dict]]] = defaultdict(dict)
    if args.room and existing.exists():
        timeline.update(json.loads(existing.read_text(encoding="utf-8")))
    stats: dict[str, int] = defaultdict(int)
    failed: list[str] = []

    for room, day in keys:
        sessions = grouped[(room, day)]
        tag = f"{room.replace(' ', '_')}_{day.replace('-', '')}"
        path = transcript_path(room, day)
        print(f"{tag}  {len(sessions)} 場", end="  ")

        if not path.exists():
            print("沒有逐字稿 → 排程時間")
            rows = [{"start": s["start"], "end": s["end"], "title": s["title"],
                     "confidence": "none"} for s in sessions]
        else:
            text = path.read_text(encoding="utf-8")
            prompt = build_prompt(room, day, sessions, text)
            if args.dry_run:
                print(f"prompt {len(prompt):,} 字（約 {len(prompt) * 3 // 4:,} token）")
                continue

            cache = RAW / f"{tag}.json"
            if cache.exists() and not args.force:
                print("用快取", end="  ")
                answer = json.loads(cache.read_text(encoding="utf-8"))["sessions"]
            else:
                print(f"呼叫模型（{len(prompt):,} 字）")
                started = time.time()
                try:
                    answer = parse_answer(call_api(args.api_url, args.api_key, args.model,
                                                   prompt, args.timeout))
                except Exception as err:                     # noqa: BLE001 — 一廳失敗不該中斷全部
                    print(f"      失敗：{err}")
                    failed.append(tag)
                    answer = None
                else:
                    print(f"      {time.time() - started:.0f}s")
                    cache.write_text(json.dumps({"sessions": answer}, ensure_ascii=False, indent=2),
                                     encoding="utf-8")

            try:
                rows = validate(day, sessions, answer, coverage(text)) if answer else None
            except (ValueError, KeyError, TypeError) as err:
                print(f"      回答不合格（{err}）→ 整天退回排程時間")
                failed.append(tag)
                rows = None
            if rows is None:
                rows = [{"start": s["start"], "end": s["end"], "title": s["title"],
                         "confidence": "none"} for s in sessions]

        for r in rows:
            stats[r["confidence"]] += 1
        timeline[room][day] = [{"start": r["start"].isoformat(), "end": r["end"].isoformat(),
                                "title": r["title"], "confidence": r["confidence"]} for r in rows]

    if args.dry_run:
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "timeline.json").write_text(
        json.dumps(dict(sorted(timeline.items())), ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(stats.values())
    print(f"\n{len(keys)} 個廳-天 / {total} 場 → {OUT / 'timeline.json'}")
    for k, label in (("high", "可直接用"), ("low", "需複查"), ("none", "退回排程時間")):
        if stats[k]:
            print(f"  {k:5} {stats[k]:4}  {label}")
    if failed:
        print(f"  失敗的廳-天（已退回排程時間）：{'、'.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
