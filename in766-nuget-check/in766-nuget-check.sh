#!/usr/bin/env bash
# IN-766: сравнить dotnet CLI и NuGet.Protocol в контейнере dotnet против ProGet.
# Запуск: [REPO=<клон OpenIDE Backend>] [PROGET_USER=.. PROGET_PASSWORD=..] ./in766-nuget-check.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${IMAGE:-proget.inc.elara.local/main/library/dotnet:0.5.0}"
FEED="${FEED:-http://proget.inc.elara.local/nuget/Default/v3/index.json}"
NUGET_VERSION="${NUGET_VERSION:-6.12.1}"
REPO="${REPO:-}"
LOG="${LOG:-$PWD/in766-$(date +%Y%m%d-%H%M%S).log}"

command -v docker >/dev/null 2>&1 || { echo "[FAIL] docker не найден"; exit 2; }
docker info >/dev/null 2>&1 || { echo "[FAIL] docker недоступен: нет прав или не запущен"; exit 2; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull "$IMAGE" \
    || { echo "[FAIL] не скачать $IMAGE. docker login proget.inc.elara.local"; exit 2; }

MOUNT=(-v "$HERE:/src:ro")
if [ -n "$REPO" ]; then
    [ -d "$REPO" ] || { echo "[FAIL] нет каталога REPO=$REPO"; exit 2; }
    MOUNT+=(-v "$(cd "$REPO" && pwd):/repo:ro")
fi
echo "[INFO] образ $IMAGE, фид $FEED, NuGet $NUGET_VERSION${REPO:+, репозиторий $REPO}"
docker run --rm "${MOUNT[@]}" \
    -e FEED="$FEED" -e NUGET_VERSION="$NUGET_VERSION" \
    -e PROGET_USER="${PROGET_USER:-}" -e PROGET_PASSWORD="${PROGET_PASSWORD:-}" \
    -e DOTNET_CLI_TELEMETRY_OPTOUT=1 -e DOTNET_NOLOGO=1 \
    --entrypoint bash "$IMAGE" /src/inside.sh 2>&1 | tee "$LOG"
echo "[INFO] лог: $LOG"
