# COSCUP 剪輯時間線廣播 — deploy

把整個資料夾複製到 deploy 機器，就這兩個檔案。

```
compose.yaml      docker compose 設定
timeline.json     353 場議程的切點（372K）— 選用，image 裡已經有一份
```

## 最省事：只要 compose.yaml

`timeline.json` 已經烤進 image，所以連資料檔都不用帶。

```bash
docker compose pull
docker compose up -d
curl localhost:3000/api/v1/health
```

打開 `http://<這台機器>:3000` 會列出全部 54 個「廳-天」組合。

## 在 COSCUP Cut 裡

| 欄位 | 填什麼 |
| --- | --- |
| Server URL | `http://<這台機器>:3000` |
| 廳 ID | `TR409-2@0809`（從網頁點一下就複製） |

**廳 ID 一定要帶日期。** 編輯器匯入時會抓該廳全部時間點，
只要有一段落在目前載入影片的範圍外，匯出鈕會整個鎖住。
載入哪天的 OBS 檔就用哪天的 ID。

其他寫法：`@2026-08-09` 完整日期、`@2` 第二天、
`!high` 只要有把握的場次（例：`TR313@0808!high`）。

## 想換資料又不重 build image

把 `timeline.json` 放在 `compose.yaml` 旁邊，解開 compose 裡那兩行註解：

```yaml
    environment:
      TIMELINE_PATH: /data/timeline.json
    volumes:
      - ./timeline.json:/data/timeline.json:ro
```

server 會比對 mtime 自動重載，換掉檔案不用重啟容器。

## Image

```
image.prod.tw/coscup-transcript-timeline:latest
image.prod.tw/coscup-transcript-timeline:20260815-1603
```

`linux/amd64` + `linux/arm64`，75 MB，python:3.13-alpine，純標準函式庫零相依，
非 root（uid 10001）執行。

要 pull 得先 `docker login image.prod.tw`。

## 環境變數

| 變數 | 預設 | 用途 |
| --- | --- | --- |
| `PORT` | `3000` | 容器內監聽 port |
| `HOST` | `0.0.0.0` | 監聽位址 |
| `TIMELINE_PATH` | `/app/out/timeline.json` | 資料來源 |
| `MIN_CONFIDENCE` | `none` | 全域信心門檻，設 `high` 就只給有把握的場次 |

compose 裡對外的 port 用 `PORT` 環境變數覆寫：`PORT=3100 docker compose up -d`。

## API

```
GET /                                 首頁，列出全部廳 ID
GET /api/v1/health
GET /api/v1/rooms                     全部廳-天組合
GET /api/v1/rooms/{廳ID}/events        COSCUP Cut 匯入用
GET /api/v1/rooms/{廳ID}/sessions      附標題，對照哪段是哪場
```

## 資料涵蓋範圍

353 場議程 / 54 個廳-天組合 / 706 個時間點：

- `high` 230 場 — 從逐字稿推導，可直接用
- `low` 14 場 — 逐字稿只覆蓋一部分，剪之前看一下
- `none` 109 場 — 該時段完全沒錄到，用的是官方排程時間

首頁表格的「狀態」欄會標出哪些廳-天需要複查。
