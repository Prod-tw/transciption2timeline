#!/usr/bin/env python3
"""validate() 的自我檢查：python3 _timeline/test_build_timeline.py"""

import datetime as dt

from build_timeline import TZ, validate

DAY = "2026-08-09"
COVER = ("09:00:00", "17:00:00")


def sess(h1, h2, title):
    at = lambda h: dt.datetime.strptime(f"{DAY} {h}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    return {"start": at(h1), "end": at(h2), "title": title}


SESSIONS = [sess("09:30:00", "10:00:00", "A"), sess("10:10:00", "10:40:00", "B")]


def rows(answer, cover=COVER):
    return validate(DAY, SESSIONS, answer, cover)


# 正常
r = rows([{"index": 1, "start": "09:28:00", "end": "10:02:00", "confidence": "high"},
          {"index": 2, "start": "10:12:00", "end": "10:41:00", "confidence": "high"}])
assert [x["confidence"] for x in r] == ["high", "high"], r
assert r[0]["start"].strftime("%H:%M") == "09:28" and r[0]["title"] == "A"

# start >= end、超出涵蓋範圍、confidence none → 退回排程時間
r = rows([{"index": 1, "start": "10:02:00", "end": "09:28:00", "confidence": "high"},
          {"index": 2, "start": "", "end": "", "confidence": "none"}])
assert [x["confidence"] for x in r] == ["none", "none"]
assert r[0]["start"] == SESSIONS[0]["start"]

r = rows([{"index": 1, "start": "08:00:00", "end": "09:50:00", "confidence": "high"},
          {"index": 2, "start": "10:12:00", "end": "10:41:00", "confidence": "high"}])
assert r[0]["confidence"] == "none" and r[0]["start"] == SESSIONS[0]["start"]

# 與排程差超過 30 分鐘 → low
r = rows([{"index": 1, "start": "09:28:00", "end": "11:20:00", "confidence": "high"},
          {"index": 2, "start": "11:25:00", "end": "11:40:00", "confidence": "high"}])
assert r[0]["confidence"] == "low", r

# 小幅重疊 → 夾到中點，兩場都降 low
r = rows([{"index": 1, "start": "09:28:00", "end": "10:20:00", "confidence": "high"},
          {"index": 2, "start": "10:10:00", "end": "10:41:00", "confidence": "high"}])
assert r[0]["end"] == r[1]["start"] == dt.datetime(2026, 8, 9, 10, 15, tzinfo=TZ), r
assert [x["confidence"] for x in r] == ["low", "low"]

# 兩場答反 → 依時間重新對應，兩場降 low
r = rows([{"index": 1, "start": "10:12:00", "end": "10:41:00", "confidence": "high"},
          {"index": 2, "start": "09:28:00", "end": "10:02:00", "confidence": "high"}])
assert [x["confidence"] for x in r] == ["low", "low"], r
assert r[0]["start"].strftime("%H:%M") == "09:28" and r[1]["end"].strftime("%H:%M") == "10:41"

# 三場輪轉（TR412-2 0808 的真實情況）→ 排序後回到議程順序
three = SESSIONS + [sess("10:50:00", "11:20:00", "C")]
r = validate(DAY, three, [{"index": 1, "start": "10:12:00", "end": "10:41:00", "confidence": "high"},
                          {"index": 2, "start": "10:50:00", "end": "11:19:00", "confidence": "high"},
                          {"index": 3, "start": "09:28:00", "end": "10:02:00", "confidence": "high"}],
             COVER)
assert [x["start"].strftime("%H:%M") for x in r] == ["09:28", "10:12", "10:50"], r
assert [x["confidence"] for x in r] == ["low", "low", "low"]

# none 的場次不參與重新對應，維持排程時間
r = rows([{"index": 1, "start": "", "end": "", "confidence": "none"},
          {"index": 2, "start": "10:12:00", "end": "10:41:00", "confidence": "high"}])
assert r[0]["start"] == SESSIONS[0]["start"] and r[1]["confidence"] == "high"

# index 缺漏 / 重複 → 整天作廢
for bad in ([{"index": 1, "start": "09:28:00", "end": "10:02:00", "confidence": "high"}],
            [{"index": 1, "start": "09:28:00", "end": "10:02:00", "confidence": "high"}] * 2):
    try:
        rows(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"應該要 raise：{bad}")

print("ok")
