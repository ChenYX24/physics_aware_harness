#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
H3_MODEL_VARIANT=ref2va H3_PORT=30011 exec "$here/minimax_h3_ref2va.sh" "${1:-launch}"
