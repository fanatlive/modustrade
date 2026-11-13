#!/usr/bin/env bash
# Local demo only. Own and stop only the worker started by this script.
set -euo pipefail
cd "$(dirname "$0")/.."
export DEBUG=1 DEMO_MODE=1
export DB_ENGINE="${DB_ENGINE:-sqlite}"
port="${PORT:-8000}"
python_bin="${PYTHON_BIN:-python3}"
venv_dir="${VENV_DIR:-.venv}"
if [[ ! -x "$venv_dir/bin/python" ]]; then "$python_bin" -m venv "$venv_dir"; fi
py="$venv_dir/bin/python"
"$py" -c 'import socket,sys; s=socket.socket(); s.bind(("127.0.0.1",int(sys.argv[1]))); s.close()' "$port"
"$py" -m pip install --disable-pip-version-check -r requirements.txt
"$py" manage.py migrate --noinput
"$py" manage.py load_demo
"$py" manage.py load_hackathon_demo
"$py" manage.py load_pavlodar_demo
"$py" manage.py collectstatic --noinput
"$py" manage.py check
if [[ "${1:-}" == '--prepare-only' ]]; then exit 0; fi
mkdir -p .local
"$py" manage.py process_outbox --loop > .local/dev-worker.log 2>&1 &
worker_pid=$!
trap 'kill "$worker_pid" 2>/dev/null || true; wait "$worker_pid" 2>/dev/null || true' EXIT
"$py" manage.py runserver "127.0.0.1:$port" --noreload
