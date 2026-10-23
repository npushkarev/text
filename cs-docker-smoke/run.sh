#!/usr/bin/env bash
# Пробный запуск автотестов cs_auto_test против КС в Docker, без железа и host-сети.
# Запуск: CS_TAG=<тег> PY_IMAGE=<образ python> ./run.sh [путь к cs_auto_test]
# Подробности: README.md рядом.
set -uo pipefail

CS_IMAGE="${CS_IMAGE:-proget.inc.elara.local/main/library/cs_product}"
CS_TAG="${CS_TAG:-}"
PY_IMAGE="${PY_IMAGE:-}"
PIP_INDEX_URL="${PIP_INDEX_URL:-}"
PYTEST_K="${PYTEST_K:-test_ip}"
PYTEST_ARGS="${PYTEST_ARGS:-}"
CS_PORTS="${CS_PORTS:-}"
WAIT_SECONDS="${WAIT_SECONDS:-120}"
INTERNAL="${INTERNAL:-1}"
GEN_PROTO="${GEN_PROTO:-0}"
KEEP="${KEEP:-0}"
REPO_URL="${REPO_URL:-https://bitbucket.inc.elara.local/scm/su2/cs_auto_test.git}"
REPO_BRANCH="${REPO_BRANCH:-develop}"
OUT="${OUT:-$PWD/cs-smoke-out}"

say()  { echo "[INFO] $*"; }
warn() { echo "[WARN] $*" >&2; }
fail() { echo "[FAIL] $*" >&2; exit 2; }

[ -n "$CS_TAG" ]   || fail "задайте CS_TAG: тег образа КС из ProGet (All Tags)"
[ -n "$PY_IMAGE" ] || fail "задайте PY_IMAGE: образ python из ProGet, например proget.inc.elara.local/main/library/python:<версия>"
command -v docker >/dev/null 2>&1 || fail "docker не найден"
docker info >/dev/null 2>&1 || fail "docker недоступен: нет прав или не запущен (sudo usermod -aG docker \$USER, затем перелогиниться)"

REPO="${1:-}"
if [ -z "$REPO" ]; then
    REPO="$PWD/cs_auto_test"
    if [ ! -d "$REPO/.git" ]; then
        say "клонирую $REPO_URL ($REPO_BRANCH) в $REPO"
        git clone --branch "$REPO_BRANCH" --depth 1 "$REPO_URL" "$REPO" || fail "git clone не удался"
    fi
fi
REPO="$(cd "$REPO" 2>/dev/null && pwd)" || fail "нет каталога с тестами: ${1:-}"
[ -f "$REPO/requirements.txt" ] || fail "в $REPO нет requirements.txt: это не cs_auto_test?"

if [ -e "$OUT" ] && [ -n "$(ls -A "$OUT" 2>/dev/null)" ]; then
    OUT="$OUT-$(date +%Y%m%d-%H%M%S)"
fi
mkdir -p "$OUT" || fail "не могу создать $OUT"

ID="cs-smoke-$$"
NET="$ID-net"
CS="$ID-cs"
TEST_IMAGE="$ID-tests:local"

cleanup() {
    docker logs "$CS" > "$OUT/cs.log" 2>&1 || true
    docker inspect --format '{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} error={{.State.Error}}' \
        "$CS" > "$OUT/cs.state" 2>&1 || true
    if [ "$KEEP" = "1" ]; then
        warn "KEEP=1: оставляю контейнер $CS, сеть $NET и образ $TEST_IMAGE"
        return
    fi
    docker rm -f "$CS" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    docker image rm "$TEST_IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

say "КС:       $CS_IMAGE:$CS_TAG"
say "python:   $PY_IMAGE"
say "тесты:    $REPO, pytest -k \"$PYTEST_K\""
say "сеть:     $NET (internal=$INTERNAL)"
say "отчёты:   $OUT"

say "1/5 скачиваю образы"
pull() { docker image inspect "$1" >/dev/null 2>&1 || docker pull "$1"; }
pull "$CS_IMAGE:$CS_TAG" || fail "не скачать КС. docker login proget.inc.elara.local и проверьте тег"
pull "$PY_IMAGE" || fail "не скачать $PY_IMAGE"

say "2/5 собираю образ тестов (зависимости из requirements.txt)"
BUILD="$(mktemp -d)"
cp "$REPO/requirements.txt" "$BUILD/"
cat > "$BUILD/Dockerfile" <<DOCKERFILE
FROM $PY_IMAGE
ARG PIP_INDEX_URL
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt /tmp/requirements.txt
RUN if [ -n "\$PIP_INDEX_URL" ]; then set -- --index-url "\$PIP_INDEX_URL"; fi; \\
    python3 -m pip install --no-cache-dir "\$@" -r /tmp/requirements.txt
DOCKERFILE
docker build -q -t "$TEST_IMAGE" --build-arg PIP_INDEX_URL="$PIP_INDEX_URL" "$BUILD" > "$OUT/build.log" 2>&1
rc=$?
rm -rf "$BUILD"
[ $rc -eq 0 ] || { cat "$OUT/build.log" >&2; fail "pip install не прошёл: см. $OUT/build.log. Нужен PIP_INDEX_URL на PyPI-фид ProGet?"; }

say "3/5 запускаю КС без privileged, host-сети и устройств"
NET_OPTS=""
[ "$INTERNAL" = "1" ] && NET_OPTS="--internal"
docker network create $NET_OPTS "$NET" >/dev/null || fail "не создать сеть $NET"
docker run -d --name "$CS" --network "$NET" --network-alias cs "$CS_IMAGE:$CS_TAG" >/dev/null \
    || fail "КС не запустилась"

say "4/5 жду готовности КС (до ${WAIT_SECONDS}с)"
deadline=$(( $(date +%s) + WAIT_SECONDS ))
if [ -n "$CS_PORTS" ]; then
    until docker run --rm --network "$NET" "$TEST_IMAGE" python3 -c "
import socket, sys
for p in '$CS_PORTS'.split(','):
    s = socket.socket(); s.settimeout(2)
    try: s.connect(('cs', int(p)))
    except OSError: sys.exit(1)
    finally: s.close()
" >/dev/null 2>&1; do
        [ "$(docker inspect -f '{{.State.Running}}' "$CS" 2>/dev/null)" = "true" ] \
            || { docker logs --tail 50 "$CS" >&2; fail "КС остановилась, см. $OUT/cs.log"; }
        [ "$(date +%s)" -lt "$deadline" ] \
            || { docker logs --tail 50 "$CS" >&2; fail "порты $CS_PORTS не открылись за ${WAIT_SECONDS}с"; }
        sleep 3
    done
    say "порты $CS_PORTS открыты"
else
    warn "CS_PORTS не задан: просто жду 20с и проверяю, что КС жива"
    sleep 20
    [ "$(docker inspect -f '{{.State.Running}}' "$CS" 2>/dev/null)" = "true" ] \
        || { docker logs --tail 50 "$CS" >&2; fail "КС остановилась, см. $OUT/cs.log"; }
fi

say "5/5 запускаю pytest"
HTML=""
if docker run --rm "$TEST_IMAGE" python3 -c "import pytest_html" >/dev/null 2>&1; then
    HTML="--html=/out/report.html --self-contained-html"
else
    warn "pytest-html не установлен: будет только junit"
fi
PROTO_CMD=""
if [ "$GEN_PROTO" = "1" ]; then
    PROTO_CMD='python3 -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. --pyi_out=. *.proto && '
fi
WORK="$(mktemp -d)"
cp -a "$REPO/." "$WORK/"
rm -rf "$WORK/.git"
docker run --rm --network "$NET" \
    --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$WORK:/test" -v "$OUT:/out" -w /test \
    "$TEST_IMAGE" sh -c "${PROTO_CMD}python3 -m pytest -v -s -k \"$PYTEST_K\" --device-ip=cs \
        --junitxml=/out/junit.xml $HTML $PYTEST_ARGS" 2>&1 | tee "$OUT/pytest.log"
rc=${PIPESTATUS[0]}
rm -rf "$WORK"

echo
case $rc in
    0) say "ТЕСТЫ ПРОШЛИ" ;;
    1) warn "есть упавшие тесты, см. $OUT/pytest.log" ;;
    4) warn "pytest: ошибка аргументов. Есть ли опция --device-ip в conftest.py этой ветки?" ;;
    5) warn "pytest: ни один тест не выбран по -k \"$PYTEST_K\"" ;;
    *) warn "pytest завершился с кодом $rc, см. $OUT/pytest.log" ;;
esac
say "логи и отчёты: $OUT (cs.log, cs.state, build.log, pytest.log, junit.xml, report.html)"
exit $rc
