#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
H3_GPU_LIST="${H3_GPU_LIST:-4,5,6,7}" H3_MODEL_VARIANT=fl2va H3_PORT=30010 \
  exec "$here/minimax_h3_ref2va.sh" "${1:-launch}"
