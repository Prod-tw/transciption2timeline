# 從逐字稿產生剪輯時間線

現場按鈕（`coscup-timeline` 的 `POST /api/v1/events`）記錄的時間點太少，
這裡改用每間教室的 Rozeta 逐字稿推導每場議程的實際起訖時間，
再推進同一台 `coscup-time-server`，讓 `COSCUP Cut` 用原本的方式匯入。

## 資料從哪來

| 來源 | 用途 |
| --- | --- |
| `../manifest.json` + `../<廳>/<session>/transcriptions.json` | 每句話的絕對時間 `created_at`（UTC 秒） |
| `opass.json`（<https://coscup.org/2026/api/opass.json>） | 官方議程：353 場、每場的廳與排程時間 |

Rozeta 每間教室其實只有「一天一條」連續的錄音流，
meeting 的標題只是當下設定的那場議程，`ended_at` 是排程結束時間、不是真的講完，
所以標題和 metadata 都不能直接當切點，只有 `created_at` 是可靠的絕對時間。

## 怎麼推導的

分三步，中間那步交給模型：

```
1. build_transcripts.py   逐字稿 → 一廳一天一個 .txt
2. build_timeline.py      議程表 + 逐字稿 → 模型 → timeline.json
3. serve_timeline.py      timeline.json → COSCUP Cut 可匯入的 server
```

1. **整理逐字稿**：同一廳同一天所有 Rozeta session 合併去重，30 秒內的句子併成一行，
   超過 30 秒的空檔直接標成 `--- 靜默 N s (HH:MM:SS → HH:MM:SS) ---`。
   630 萬字壓成 400 萬字，中位數 9.3 萬字、最大 20 萬字（≈ 15 萬 token）。
   靜默標記是給模型最重要的訊號 —— 換場點直接看得見，不必自己算時間差。
2. **問模型**：一個「廳-天」一次呼叫，把當天的議程表 + 整天的逐字稿一起送進去，
   模型回每一場的實際起訖時間與 `confidence`。用 `index` 對回議程表，標題由程式帶入，
   不讓模型重打（避免抄錯字）。
3. **程式端驗證**（模型答錯要抓得出來）：

   | 檢查 | 不通過時 |
   | --- | --- |
   | 回傳筆數 == 議程場數、`index` 不重複不缺漏 | 該廳-天整天退回排程時間 |
   | `HH:MM:SS` 格式正確、`start < end` | 該場退回排程時間，`confidence: none` |
   | 時間落在該天逐字稿的涵蓋範圍內 | 同上 |
   | 依議程順序不重疊 | 重疊處夾到中點，降為 `low` |
   | 與排程時間相差超過 30 分鐘 | 降為 `low`（不改時間，留給人複查） |

4. 該廳該天完全沒有逐字稿的，不呼叫模型，直接用官方排程時間、`confidence: none`。

提示詞裡明講的幾件事：時間戳是「句子結束」時間所以起點要往前抓；教室麥克風整天開著，
開場前的測麥閒聊不算議程內容；切點寧可偏早 —— 偏早只是多剪到休息時間，偏晚會吃掉講者開場。

## 檔案

```
build_transcripts.py  逐字稿整理，產出 transcripts/
build_timeline.py     呼叫模型推導時間軸
serve_timeline.py     廣播 server：一台同時提供全部教室（建議用這個）
push_to_server.py     把切點推進原本那台 coscup-time-server
Dockerfile            廣播 server 的 image
compose.yaml          deploy 機器上用
docker-push.sh        build + push 到 image.prod.tw
opass.json            官方議程快取
transcripts/
  <廳>_<YYYYMMDD>.txt        餵給模型的逐字稿，46 個檔
out/
  raw/<廳>_<YYYYMMDD>.json   模型原始回答（快取，重跑會跳過，--force 覆寫）
  timeline.json              廳 → 日期 → [{start, end, title, confidence}]
```

`timeline.json` 長這樣：

```json
{
  "TR409-2": {
    "2026-08-09": [
      {"start": "2026-08-09T09:02:11+08:00",
       "end":   "2026-08-09T09:48:30+08:00",
       "title": "從 0 開始的 Kubernetes",
       "confidence": "high"}
    ]
  }
}
```

`confidence` 三級：

- `high` — 逐字稿裡找得到明確的起訖訊號，可直接用
- `low` — 訊號模糊、錄音頭尾不完整，或與排程差超過 30 分鐘，請人工確認
- `none` — 該時段完全沒錄到（或模型答案不合格），用的是官方排程時間

## 怎麼用（建議：廣播 server）

```bash
# 1. 整理逐字稿（原始資料沒變就不用重跑）
python3 _timeline/build_transcripts.py

# 2. 問模型推導時間軸（需要 API key）
API_KEY=<gcli2api 的 PASSWORD> python3 _timeline/build_timeline.py

# 3. 一台 server 廣播全部教室
python3 _timeline/serve_timeline.py --port 3000
```

打開 <http://localhost:3000> 會列出全部 54 個「廳-天」組合，點 ID 直接複製。
在 COSCUP Cut：

| 欄位 | 填什麼 |
| --- | --- |
| Server URL | `http://localhost:3000` |
| 廳 ID | `TR409-2@0809` |

換一支影片就換一次廳 ID，不用重推任何東西。`timeline.json` 改了會自動重載。

廳 ID 的寫法（大小寫、分隔符號都不挑）：

```
TR409-2@0809            月日
TR409-2@2026-08-09      完整日期
TR409-2@2               第 2 天
TR409-2                 只有一天有資料時可以省略
209@0809                純數字自動補 TR 前綴
TR409-2@0809!high       只要信心 high 的場次
```

API 是 coscup-time-server 的唯讀相容子集：

```
GET /api/v1/health
GET /api/v1/rooms                     全部廳-天組合
GET /api/v1/rooms/{廳ID}/events        COSCUP Cut 匯入用
GET /api/v1/rooms/{廳ID}/sessions      附標題，給你對照哪段是哪場
```

### 為什麼廳 ID 要帶日期

COSCUP Cut 匯入時會抓該廳**全部**時間點，排序後 1/3/5 當開始、2/4/6 當結束；
而且 `App.tsx:261` 的匯出鈕在 `unmappedClips.length > 0` 時會整個鎖住——
只要有任何一段落在目前載入的影片範圍外，整批都不能匯出。
所以一個廳 ID 只含一天，載入哪天的 OBS 檔就用哪天的 ID。

## Docker

`timeline.json` 只有幾十 K，直接烤進 image，**deploy 機器不用放任何資料檔**。

```bash
# 先確定資料是最新的
python3 _timeline/build_timeline.py

# 登入 registry
docker login image.prod.tw

# build + push（linux/amd64 + linux/arm64，同時打 latest 與日期 tag）
./_timeline/docker-push.sh

# 只想先看指令 / 只 build 到本機
./_timeline/docker-push.sh --dry-run
./_timeline/docker-push.sh --local
```

registry 底下有 namespace 的話：

```bash
IMAGE_NS=prod-tw ./_timeline/docker-push.sh
# → image.prod.tw/prod-tw/coscup-transcript-timeline:latest
```

其他可覆寫的環境變數：`REGISTRY`、`IMAGE_NAME`、`TAG`、`PLATFORMS`。

### deploy 機器上

把 `compose.yaml` 複製過去，其他什麼都不用帶：

```bash
docker compose pull && docker compose up -d
curl localhost:3000/api/v1/health
```

或不用 compose：

```bash
docker run -d --name coscup-transcript-timeline --restart unless-stopped \
  -p 3000:3000 image.prod.tw/coscup-transcript-timeline:latest
```

### 要放哪些 data 過去

| 情境 | 要帶的檔案 |
| --- | --- |
| 一般 deploy | **無**，`timeline.json` 已在 image 裡 |
| 想換資料但不重 build | 只要 `out/timeline.json` |
| 想在 deploy 機器上重跑分析 | 整包原始逐字稿（120M）+ `build_timeline.py` + `opass.json`，不建議 |

第二種掛法：

```bash
docker run -d -p 3000:3000 \
  -v $PWD/timeline.json:/data/timeline.json:ro \
  -e TIMELINE_PATH=/data/timeline.json \
  image.prod.tw/coscup-transcript-timeline:latest
```

server 會比對 mtime 自動重載，換掉掛進去的檔案不用重啟容器。

### 容器環境變數

| 變數 | 預設 | 用途 |
| --- | --- | --- |
| `PORT` | `3000` | 監聽 port |
| `HOST` | `0.0.0.0` | 監聽位址 |
| `TIMELINE_PATH` | `/app/out/timeline.json` | 資料來源 |
| `MIN_CONFIDENCE` | `none` | 全域信心門檻 |

## 另一種用法：推進真正的 coscup-time-server

如果你想用原本那台 Axum server（可以在 dashboard 上手動改時間），
`push_to_server.py` 會把切點寫進去：

```bash
python3 _timeline/push_to_server.py TR409-2 2026-08-09 --dry-run   # 先看
python3 _timeline/push_to_server.py TR409-2 2026-08-09 --replace   # 清掉舊的再推
python3 _timeline/push_to_server.py TR313 2026-08-08 --replace --min-confidence high
```

這條路每換一天就要 `--replace` 重推一次，所以平常用廣播 server 比較省事。

## 已知限制

- 逐字稿本身有起訖：Rozeta 是人工開的，很多廳當天開得晚或中途停掉。
  落在逐字稿邊緣的場次模型會標 `low`，這種要手動拉。
- 教室麥克風整天開著，休息時間的閒聊也會進逐字稿，模型可能把它當成議程內容；
  「與排程差 30 分鐘就降 `low`」是擋這件事的最後一道防線。
- `TR309 教室外走廊`、`TR409 教室外走廊`、`TR310-2` 沒有 Rozeta 帳號，只有排程時間。
- 沒有 fallback：舊的關鍵字演算法已整支刪除，沒有 API 就跑不出 timeline。

## build_timeline.py 的參數

| 旗標 | 環境變數 | 預設 | 用途 |
| --- | --- | --- | --- |
| `--api-url` | `API_URL` | `http://192.168.1.231:7861/antigravity/v1` | OpenAI 相容端點（Antigravity 憑證那組） |
| `--api-key` | `API_KEY` | 無（必填） | gcli2api 的 `PASSWORD` |
| `--model` | `MODEL` | `gemini-3.7-flash-medium` | |
| `--room` | — | 全部 | 只跑一間教室 |
| `--force` | — | 關 | 忽略 `out/raw/` 的快取重跑 |
| `--dry-run` | — | 關 | 只印每個廳-天的 prompt 大小，不呼叫 API |
| `--timeout` | — | `600` | 單次呼叫逾時（秒） |

gcli2api 相容 OpenAI 格式：`POST {api_url}/chat/completions`、`Authorization: Bearer <PASSWORD>`。
`GET {api_url}/models` 可以先確認模型名稱可用。遇到 429 / 5xx 會自動退避重試三次（10s / 30s / 60s）。

一個廳-天失敗不會中斷整批：該天退回排程時間、印在最後的失敗清單裡，process exit code 為 1。
