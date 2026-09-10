#!/usr/bin/env bash
set +e
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd "${SCRIPT_DIR}/../.." && pwd)
LOG_DIR="${PROJECT_DIR}/itof/online/logs"
mkdir -p "${LOG_DIR}"
PID_FILE="${LOG_DIR}/runtimedisplay_control_${USER:-$(id -un)}.pid"
LOG_FILE="${LOG_DIR}/runtimedisplay_control_${USER:-$(id -un)}.log"

old=$(cat "${PID_FILE}" 2>/dev/null)
if [ -n "$old" ]; then
  kill "$old" 2>/dev/null || true
fi
for pid in $(pgrep -f 'control_server.py'); do
  if [ "$pid" != "$$" ]; then
    kill "$pid" 2>/dev/null || true
  fi
done
cd "${PROJECT_DIR}" || exit 1
chmod +x itof/online/run_control_demo.sh itof/online/control_server.py
nohup ./itof/online/run_control_demo.sh "${1:-8088}" >"${LOG_FILE}" 2>&1 </dev/null &
echo $! >"${PID_FILE}"
echo "Control page: http://127.0.0.1:${1:-8088}/"
echo "Log: ${LOG_FILE}"
