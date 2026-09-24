#!/bin/sh
# Zorgt dat het gemounte datavolume schrijfbaar is en laat daarna root-rechten vallen.
set -e

DATA_DIR="${DATA_DIR:-/app/data}"
mkdir -p "$DATA_DIR/cache" "$DATA_DIR/tmp"

if [ "$(id -u)" = "0" ]; then
    chown -R appuser:appuser "$DATA_DIR" 2>/dev/null || \
        echo "Waarschuwing: kon eigenaar van $DATA_DIR niet aanpassen" >&2
    exec setpriv --reuid=appuser --regid=appuser --init-groups "$@"
fi

exec "$@"
