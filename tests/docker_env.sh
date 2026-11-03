#!/bin/sh
# Тестовое окружение: настоящий rootless Docker Engine и Compose без root.
# © 2026 Serhii Khomenko · HomenS.AI
#
# Использование: tests/docker_env.sh install|start|stop|status|run -- КОМАНДА...
#   install  скачать статические бинарники Docker и Compose, проверить SHA-256;
#   start    запустить dockerd-rootless.sh (идемпотентно) и вывести пути/хэши;
#   stop     остановить демон этого окружения (идемпотентно);
#   status   вывести пути и состояние;
#   run      выполнить команду в отдельных user/mount namespace, где Compose
#            виден модулю как системный plugin (/usr/local/lib/docker/cli-plugins).
# HOMENSAI_COMPOSE_VERSION выбирает Compose (по умолчанию 2.40.3; есть 5.7.0).
# Не трогает /usr/bin/docker (podman-docker), Podman и системные службы.
set -eu

ROOT=${HOMENSAI_DOCKER_ROOT:-$HOME/.cache/homensai-docker}
: "${XDG_RUNTIME_DIR:?нужен XDG_RUNTIME_DIR}"
DOCKER_VERSION=29.9.2
DOCKER_TGZ_SHA256=9ac225999fd5e863f7694c728ef23d480616b1e5639328fa309befd4243d69eb
EXTRAS_TGZ_SHA256=f2663b230a7afc13dea88fc589685331d01f5f3fc458b92ab40e2388db452ed2
# Compose: модуль сейчас принимает только v2 (docker_lifecycle.py); v5 — для проверки.
COMPOSE_V2=2.40.3 COMPOSE_V2_SHA256=dba9d98e1ba5bfe11d88c99b9bd32fc4a0624a30fafe68eea34d61a3e42fd372
COMPOSE_V5=5.7.0 COMPOSE_V5_SHA256=7758048ad38e580f22559603312874fec8bb638d021a9e64182385fc17cc12a2
COMPOSE_VERSION=${HOMENSAI_COMPOSE_VERSION:-$COMPOSE_V2}
case "$COMPOSE_VERSION" in
    "$COMPOSE_V2"|"$COMPOSE_V5") ;;
    *) echo "docker_env: HOMENSAI_COMPOSE_VERSION: $COMPOSE_V2 или $COMPOSE_V5" >&2; exit 1 ;;
esac
BIN=$ROOT/bin
CLI=$BIN/docker
SOCK=$XDG_RUNTIME_DIR/homensai-docker.sock
EXEC_ROOT=$XDG_RUNTIME_DIR/homensai-docker-exec
STATE_DIR=$XDG_RUNTIME_DIR/homensai-dockerd-rootless
PIDFILE=$ROOT/rootlesskit.pid
LOG=$ROOT/dockerd.log
# Отдельная конфигурация CLI для ручной работы; модуль её не использует.
export DOCKER_CONFIG="$ROOT/config"

die() { echo "docker_env: $*" >&2; exit 1; }

check_sha() { # файл ожидаемый_хэш
    [ "$(sha256sum "$1" | cut -d' ' -f1)" = "$2" ] || die "SHA-256 не совпал: $1"
}

install_compose() { # версия хэш
    dir=$ROOT/plugins/$1/docker/cli-plugins
    [ -x "$dir/docker-compose" ] && return 0
    rel=https://github.com/docker/compose/releases/download/v$1
    mkdir -p "$ROOT/dl/v$1" "$dir"
    (cd "$ROOT/dl/v$1"
     [ -f docker-compose-linux-x86_64 ] || curl -sSfLO "$rel/docker-compose-linux-x86_64"
     curl -sSfL "$rel/docker-compose-linux-x86_64.sha256" | sha256sum -c - >/dev/null \
         || die "Compose $1 не совпал с опубликованным .sha256"
     check_sha docker-compose-linux-x86_64 "$2")
    # Копия, а не ссылка: модуль требует st_nlink == 1 и запрещает symlink.
    install -m 0755 "$ROOT/dl/v$1/docker-compose-linux-x86_64" "$dir/docker-compose"
}

install_bins() {
    if ! [ -x "$CLI" ] || ! [ -x "$BIN/dockerd-rootless.sh" ]; then
        base=https://download.docker.com/linux/static/stable/x86_64
        mkdir -p "$ROOT/dl" "$BIN"
        (cd "$ROOT/dl"
         for f in docker-$DOCKER_VERSION.tgz docker-rootless-extras-$DOCKER_VERSION.tgz; do
             [ -f "$f" ] || curl -sSfLO "$base/$f"
         done
         # download.docker.com не публикует .sha256 для static; хэши закреплены здесь.
         check_sha docker-$DOCKER_VERSION.tgz "$DOCKER_TGZ_SHA256"
         check_sha docker-rootless-extras-$DOCKER_VERSION.tgz "$EXTRAS_TGZ_SHA256"
         tar -xzf docker-$DOCKER_VERSION.tgz -C "$BIN" --strip-components=1
         tar -xzf docker-rootless-extras-$DOCKER_VERSION.tgz -C "$BIN" --strip-components=1)
    fi
    install_compose "$COMPOSE_V2" "$COMPOSE_V2_SHA256"
    install_compose "$COMPOSE_V5" "$COMPOSE_V5_SHA256"
    mkdir -p "$DOCKER_CONFIG"
    rm -rf "$DOCKER_CONFIG/cli-plugins"
    cp -a "$ROOT/plugins/$COMPOSE_VERSION/docker/cli-plugins" "$DOCKER_CONFIG/cli-plugins"
}

# timeout: до готовности демона соединение с уже созданным сокетом может зависнуть.
alive() { timeout 20 "$CLI" --host "unix://$SOCK" version --format '{{.Server.Version}}' >/dev/null 2>&1; }

info() {
    echo "cli=$CLI"
    echo "cli_sha256=$(sha256sum "$CLI" | cut -d' ' -f1)"
    echo "socket=$SOCK"
    echo "compose_plugin=$ROOT/plugins/$COMPOSE_VERSION/docker/cli-plugins/docker-compose"
    if alive; then echo "state=running"; else echo "state=stopped"; fi
}

start() {
    install_bins
    if ! alive; then
        mkdir -p "$ROOT/data" "$EXEC_ROOT"
        [ -f "$ROOT/daemon.json" ] || echo '{}' > "$ROOT/daemon.json"
        # PATH: сначала бинарники окружения; slirp4netns и newuidmap — системные.
        PATH="$BIN:/usr/sbin:/usr/bin:/sbin:/bin" \
        DOCKERD_ROOTLESS_ROOTLESSKIT_STATE_DIR="$STATE_DIR" \
        DOCKERD_ROOTLESS_ROOTLESSKIT_NET=slirp4netns \
        DOCKERD_ROOTLESS_ROOTLESSKIT_PORT_DRIVER=builtin \
        setsid nohup "$BIN/dockerd-rootless.sh" \
            --host "unix://$SOCK" --data-root "$ROOT/data" --exec-root "$EXEC_ROOT" \
            --pidfile "$EXEC_ROOT/dockerd.pid" --config-file "$ROOT/daemon.json" \
            >"$LOG" 2>&1 </dev/null &
        echo $! > "$PIDFILE"
        i=0
        until alive; do
            i=$((i + 1))
            [ $i -le 40 ] || die "демон не ответил вовремя, см. $LOG"
            kill -0 "$(cat "$PIDFILE")" 2>/dev/null || die "демон завершился, см. $LOG"
            sleep 1
        done
    fi
    info
}

stop() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        pid=$(cat "$PIDFILE")
        kill -TERM "$pid"
        i=0
        while kill -0 "$pid" 2>/dev/null; do
            i=$((i + 1))
            if [ $i -gt 30 ]; then kill -KILL "$pid" 2>/dev/null || true; break; fi
            sleep 1
        done
    fi
    rm -f "$PIDFILE"
    alive && die "сокет $SOCK всё ещё отвечает (чужой процесс?)"
    echo "state=stopped"
}

run_ns() {
    install_bins
    [ "${1:-}" = "--" ] && shift
    [ $# -gt 0 ] || die "run: нужна команда"
    upper=$ROOT/ns-overlay
    mkdir -p "$upper/upper/docker" "$upper/work"
    rm -rf "$upper/upper/docker/cli-plugins"
    cp -a "$ROOT/plugins/$COMPOSE_VERSION/docker/cli-plugins" "$upper/upper/docker/"
    # Внешний userns (root -> текущий UID) нужен только для overlay-монтирования;
    # вложенный userns возвращает команде прежние UID/GID. Overlay лишь добавляет
    # plugin в стандартный системный каталог поиска Docker CLI внутри namespace.
    exec unshare --user --map-root-user --mount sh -euc '
        uid=$1 gid=$2 dir=$3; shift 3
        mount -t overlay overlay -o "lowerdir=/usr/local/lib,upperdir=$dir/upper,workdir=$dir/work" /usr/local/lib
        exec unshare --user --map-user="$uid" --map-group="$gid" "$@"' sh "$(id -u)" "$(id -g)" "$upper" "$@"
}

case "${1:-}" in
    install) install_bins; info ;;
    start) start ;;
    stop) stop ;;
    status) info ;;
    run) shift; run_ns "$@" ;;
    *) die "использование: $0 install|start|stop|status|run -- КОМАНДА..." ;;
esac
