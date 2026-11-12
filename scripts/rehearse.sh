#!/usr/bin/env bash
# Fresh local rehearsal; never reset an existing database or its history.
set -euo pipefail
cd "$(dirname "$0")/.."
rehearsal_dir="$(mktemp -d "${TMPDIR:-/tmp}/modus-rehearsal.XXXXXX")"
export SQLITE_PATH="$rehearsal_dir/rehearsal.sqlite3"
export DB_ENGINE=sqlite
export PORT="${PORT:-8766}"
printf 'Новая БД репетиции: %s\nОткройте http://127.0.0.1:%s/demo/ в двух профилях браузера.\n' "$SQLITE_PATH" "$PORT"
bash scripts/dev.sh
