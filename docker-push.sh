#!/usr/bin/env bash
# Build 並推送 COSCUP 剪輯時間線廣播 server 到 image.prod.tw
#
#   ./_timeline/docker-push.sh                 # build + push，tag latest 與日期
#   ./_timeline/docker-push.sh --dry-run       # 只印出會做什麼
#   ./_timeline/docker-push.sh --local         # 只 build 到本機，不推
#   TAG=v2 ./_timeline/docker-push.sh          # 自訂 tag
#   IMAGE_NS=prod-tw ./_timeline/docker-push.sh   # registry 底下有 namespace 時
#
# 推之前要先登入： docker login image.prod.tw

set -euo pipefail

REGISTRY="${REGISTRY:-image.prod.tw}"
IMAGE_NS="${IMAGE_NS:-}"
IMAGE_NAME="${IMAGE_NAME:-coscup-transcript-timeline}"
TAG="${TAG:-latest}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATE_TAG="$(date -u +%Y%m%d-%H%M)"

if [ -n "$IMAGE_NS" ]; then
  REPO="$REGISTRY/$IMAGE_NS/$IMAGE_NAME"
else
  REPO="$REGISTRY/$IMAGE_NAME"
fi

DRY_RUN=0
PUSH=1
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --local)   PUSH=0; PLATFORMS="" ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "不認識的參數：$arg" >&2; exit 2 ;;
  esac
done

if [ ! -f "$HERE/out/timeline.json" ]; then
  echo "找不到 $HERE/out/timeline.json" >&2
  echo "請先執行： python3 _timeline/build_timeline.py" >&2
  exit 1
fi

SESSIONS=$(python3 -c "import json;d=json.load(open('$HERE/out/timeline.json'));print(sum(len(r) for days in d.values() for r in days.values()))")
SIZE=$(du -h "$HERE/out/timeline.json" | cut -f1)

echo "repo      $REPO"
echo "tags      $TAG, $DATE_TAG"
echo "平台      ${PLATFORMS:-本機原生}"
echo "資料      ${SESSIONS} 場議程, timeline.json ${SIZE}，烤進 image"
echo

cmd=(docker buildx build
  -f "$HERE/Dockerfile"
  -t "$REPO:$TAG"
  -t "$REPO:$DATE_TAG")
[ -n "$PLATFORMS" ] && cmd+=(--platform "$PLATFORMS")
if [ "$PUSH" -eq 1 ]; then cmd+=(--push); else cmd+=(--load); fi
cmd+=("$HERE")

printf '%q ' "${cmd[@]}"; echo
if [ "$DRY_RUN" -eq 1 ]; then
  echo
  echo "--dry-run，什麼都沒做。"
  exit 0
fi

"${cmd[@]}"

echo
if [ "$PUSH" -eq 1 ]; then
  echo "已推送 $REPO:$TAG 與 $REPO:$DATE_TAG"
  echo
  echo "在 deploy 機器上："
  echo "  IMAGE=$REPO:$TAG docker compose -f compose.yaml up -d"
else
  echo "已 build 到本機 $REPO:$TAG"
  echo "  docker run --rm -p 3000:3000 $REPO:$TAG"
fi
