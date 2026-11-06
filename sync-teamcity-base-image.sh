#!/usr/bin/env bash
# Заливает базовый образ TeamCity-сервера в Docker-фид ProGet. Первая стадия
# конвейера пропатченного образа: без базы в ProGet сборке
# build-teamcity-patched-image.sh в закрытом контуре нечего патчить.
#
# Источник по умолчанию — официальный образ в Docker Hub, но можно указать
# полный ref любого другого образа с TeamCity (в том числе из другого реестра)
# через env.BASE_IMAGE. Запускать там, где есть доступ и к источнику, и к ProGet.
# Инструкция: docs/teamcity-patched-image-ci.md.
#
# Usage:
#   bash scripts/sync-teamcity-base-image.sh 2026.2 [amd64|arm64]
# Переменные TeamCity (Parameters):
#   env.BASE_IMAGE   полный ref источника (по умолчанию
#                    jetbrains/teamcity-server:<VERSION>)
#   env.REGISTRY     реестр с фидом, напр. proget-new.inc.elara.local/main;
#                    без него выполняется только pull
#   env.IMAGE_NAME   имя базового образа в ProGet (по умолчанию teamcity-server)
#   env.BASE_TAG     тег базового образа в ProGet (по умолчанию VERSION)
#   DOCKER_USER / DOCKER_PASSWORD — креды пуша (password-параметры)
#
# Локальная проверка:
#   bash scripts/sync-teamcity-base-image.sh 2026.2 amd64   # без пуша: нет REGISTRY
set -euo pipefail

VERSION=${1:?Usage: sync-teamcity-base-image.sh VERSION [arch]}
ARCH=${2:-amd64}
case "$ARCH" in amd64|arm64) ;; *) echo 'arch: amd64 или arm64' >&2; exit 2;; esac

src=${BASE_IMAGE:-jetbrains/teamcity-server:$VERSION}
IMAGE_NAME=${IMAGE_NAME:-teamcity-server}
BASE_TAG=${BASE_TAG:-$VERSION}

ok()  { printf '  [OK]   %s\n' "$*"; }
step() { printf '== %s ==\n' "$*"; }

printf '\n== Проверка ==\n'
printf '  источник: %s (linux/%s)\n  реестр:   %s\n' "$src" "$ARCH" "${REGISTRY:-не задан (только pull)}"
if [[ -z ${REGISTRY:-} && -z ${DOCKER_USER:-} ]]; then
  printf '  режим: только скачать (REGISTRY не задан — пуш пропущен)\n'
fi
docker info >/dev/null 2>&1 || { printf '  [НЕТ]  docker недоступен\n' >&2; exit 2; }
logged_in_registry=''
cleanup() {
  if [[ -n $logged_in_registry ]]; then
    docker logout "$logged_in_registry" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

step "1/3 Pull $src (linux/$ARCH)"
docker pull --platform "linux/$ARCH" "$src"
ok "базовый образ скачан"

if [[ -z ${REGISTRY:-} ]]; then
  printf '\nИтог: образ %s локально; пуш пропущен (задайте env.REGISTRY)\n' "$src"
  exit 0
fi

step "2/3 Push в $REGISTRY"
IMAGE="$REGISTRY/$IMAGE_NAME:$BASE_TAG"
if [[ -n ${DOCKER_USER:-} && -n ${DOCKER_PASSWORD:-} ]]; then
  printf '%s' "$DOCKER_PASSWORD" | docker login "${REGISTRY%%/*}" --username "$DOCKER_USER" --password-stdin
  logged_in_registry=${REGISTRY%%/*}
fi
docker tag "$src" "$IMAGE"
docker push "$IMAGE"
ok "запушен $IMAGE"

step "3/3 Logout"
if [[ -n $logged_in_registry ]]; then
  docker logout "$logged_in_registry" >/dev/null 2>&1 || true
  logged_in_registry=''
  ok 'креды из ~/.docker/config.json убраны'
else
  ok 'login не выполнялся'
fi

printf '\nИтог: %s\n' "$IMAGE"
printf 'Теперь build-teamcity-patched-image.sh возьмёт базу через env.BASE_IMAGE_REF=%s\n' "$IMAGE"
