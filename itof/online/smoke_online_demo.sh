#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_DIR=$(cd "${SCRIPT_DIR}/../.." && pwd)
ROOT_SETUP=${ROOT_SETUP:-}
INPUT_DIR=${1:-${PROJECT_DIR}/itof/data/20260519_1}
PORT=${2:-8091}
EVENTS=${3:-50}
MAP_FILE=${4:-${PROJECT_DIR}/itof/reco/map/iTOFMap_newchip2511.csv}

if [ -z "${ROOT_SETUP}" ] && [ -n "${ROOTSYS:-}" ] && [ -f "${ROOTSYS}/bin/thisroot.sh" ]; then
  ROOT_SETUP="${ROOTSYS}/bin/thisroot.sh"
fi
if [ -z "${ROOT_SETUP}" ] && ! command -v root >/dev/null 2>&1 && [ -f "${HOME}/Software/root/bin/thisroot.sh" ]; then
  ROOT_SETUP="${HOME}/Software/root/bin/thisroot.sh"
fi
set +u
if [ -n "${ROOT_SETUP}" ] && [ -f "${ROOT_SETUP}" ]; then
  source "${ROOT_SETUP}"
fi
set -u
command -v root >/dev/null 2>&1 || { echo "ROOT executable not found. Set ROOT_SETUP=/path/to/root/bin/thisroot.sh" >&2; exit 127; }

export VMCWORKDIR="${PROJECT_DIR}"
export ITOF_ONLINE_MAP="${MAP_FILE}"
export ITOF_ONLINE_CLSB="${PROJECT_DIR}/itof/macro/newchip/CLSB.txt"

cd "${PROJECT_DIR}"
root -l -b -q "itof/online/iTOFRootOnlineMonitor.C+(\"${INPUT_DIR}\",${PORT},${EVENTS},100,false,\"\")"
