#!/bin/sh
set -eu
umask 077
VERSION='@VERSION@'
SERIES='@SERIES@'
TARGET=${SYNOPKG_PKGDEST:-/var/packages/MariaDBCustom/target}
VAR=${SYNOPKG_PKGVAR:-/var/packages/MariaDBCustom/var}
DATA=$VAR/data
RUN=$VAR/run
CONF=$VAR/my.cnf
SOCKET=$RUN/mariadb.sock
PIDFILE=$RUN/mariadb.pid

check_data() {
    if [ -d "$DATA/mysql" ]; then
        if [ ! -f "$VAR/engine-series" ] || [ "$(cat "$VAR/engine-series")" != "$SERIES" ]; then
            echo "Existing data does not belong to MariaDB $SERIES." >&2
            return 1
        fi
    fi
}

prepare() {
    check_data
    mkdir -p "$DATA" "$RUN" "$VAR/log"
    chmod 700 "$DATA" "$RUN" "$VAR/log"
    if [ ! -f "$CONF" ]; then
        cat > "$CONF" <<EOF
[client]
socket=$SOCKET
port=3308
[mysqld]
basedir=$TARGET
datadir=$DATA
socket=$SOCKET
pid-file=$PIDFILE
log-error=$VAR/log/mariadb.log
bind-address=127.0.0.1
port=3308
skip-name-resolve
innodb-buffer-pool-size=64M
key-buffer-size=8M
max-connections=20
table-open-cache=128
tmp-table-size=8M
max-heap-table-size=8M
performance-schema=OFF
character-set-server=utf8mb4
collation-server=utf8mb4_unicode_ci
EOF
        chmod 600 "$CONF"
    fi
    if [ ! -d "$DATA/mysql" ]; then
        "$TARGET/scripts/mariadb-install-db" --no-defaults --basedir="$TARGET" --datadir="$DATA" \
            --auth-root-authentication-method=socket --auth-root-socket-user="$(id -un)" \
            --skip-test-db > "$VAR/log/initialize.log" 2>&1 || {
                cat "$VAR/log/initialize.log" >&2
                return 1
            }
        printf '%s\n' "$SERIES" > "$VAR/engine-series"
    fi
}

running() {
    [ -r "$PIDFILE" ] || return 1
    pid=$(cat "$PIDFILE")
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    kill -0 "$pid" 2>/dev/null || return 1
    actual=$(readlink "/proc/$pid/exe" 2>/dev/null) || return 1
    expected=$(readlink -f "$TARGET/bin/mariadbd")
    [ "$actual" = "$expected" ]
}

start_service() {
    if running; then return 0; fi
    prepare
    "$TARGET/bin/mariadbd" --defaults-file="$CONF" --basedir="$TARGET" \
        >> "$VAR/log/console.log" 2>&1 < /dev/null &
    launched=$!
    count=0
    while [ "$count" -lt 60 ]; do
        if ! kill -0 "$launched" 2>/dev/null; then
            tail -n 30 "$VAR/log/mariadb.log" "$VAR/log/console.log" >&2
            return 1
        fi
        if "$TARGET/bin/mariadb-admin" --no-defaults --socket="$SOCKET" --user=root ping >/dev/null 2>&1; then
            return 0
        fi
        count=$((count + 1))
        sleep 1
    done
    kill -TERM "$launched" 2>/dev/null || true
    echo 'MariaDB startup timed out.' >&2
    return 1
}

stop_service() {
    if ! running; then return 0; fi
    stopped_pid=$pid
    if ! "$TARGET/bin/mariadb-admin" --no-defaults --socket="$SOCKET" --user=root shutdown; then
        if running; then kill -TERM "$stopped_pid"; fi
    fi
    count=0
    while running; do
        [ "$count" -lt 60 ] || return 1
        count=$((count + 1))
        sleep 1
    done
}
