#!/usr/bin/env bash
set -euo pipefail

H3_ROOT="${H3_ROOT:-$HOME/physics_aware_harness_h3_ref2va}"
H3_VENV="${H3_VENV:-$H3_ROOT/venv}"
H3_MODEL_ID="${H3_MODEL_ID:-MiniMax/MiniMax-H3}"
H3_MODEL_VARIANT="${H3_MODEL_VARIANT:-ref2va}"
H3_GPU_LIST="${H3_GPU_LIST:-0,1,2,3}"
H3_HOST="${H3_HOST:-127.0.0.1}"
if [ -z "${H3_PORT:-}" ]; then
  [ "$H3_MODEL_VARIANT" = "ref2va" ] && H3_PORT=30011 || H3_PORT=30010
fi
H3_MIN_FREE_GIB="${H3_MIN_FREE_GIB:-100}"
H3_REF2VA_BYTES="${H3_REF2VA_BYTES:-144051182613}"
H3_DEPENDENCY_HEADROOM_GIB="${H3_DEPENDENCY_HEADROOM_GIB:-30}"
H3_SGLANG_VERSION="${H3_SGLANG_VERSION:-0.5.17}"
H3_UV="${H3_UV:-$(command -v uv || true)}"

validate_paths() {
  case "$H3_ROOT" in
    /*) ;;
    *) echo "H3_ROOT must be an absolute path" >&2; exit 1 ;;
  esac
  [ "$H3_ROOT" != "/" ] && [ "$H3_ROOT" != "$HOME" ] || {
    echo "H3_ROOT must be a dedicated subdirectory" >&2
    exit 1
  }
  case "$H3_VENV" in
    "$H3_ROOT"/*) ;;
    *) echo "H3_VENV must stay inside H3_ROOT" >&2; exit 1 ;;
  esac
}

print_config() {
  printf 'root=%s\nmodel=%s\nvariant=%s\ngpus=%s\nhost=%s\nport=%s\nmin_free_gib=%s\n' \
    "$H3_ROOT" "$H3_MODEL_ID" "$H3_MODEL_VARIANT" "$H3_GPU_LIST" "$H3_HOST" "$H3_PORT" "$H3_MIN_FREE_GIB"
}

validate_config() {
  validate_paths
  [ "$H3_MODEL_VARIANT" = "ref2va" ] || [ "$H3_MODEL_VARIANT" = "fl2va" ] || {
    echo "H3_MODEL_VARIANT must be ref2va or fl2va" >&2
    exit 1
  }
  [ "$H3_HOST" = "127.0.0.1" ] || { echo "H3_HOST must remain 127.0.0.1" >&2; exit 1; }

  local gpu unique_count
  IFS=',' read -r -a gpus <<<"$H3_GPU_LIST"
  [ "${#gpus[@]}" -eq 4 ] || { echo "H3_GPU_LIST must select exactly four unique GPUs" >&2; exit 1; }
  for gpu in "${gpus[@]}"; do
    [[ "$gpu" =~ ^[0-9]+$ ]] || { echo "H3_GPU_LIST must contain numeric GPU IDs" >&2; exit 1; }
  done
  unique_count="$(printf '%s\n' "${gpus[@]}" | sort -u | wc -l | tr -d '[:space:]')"
  [ "$unique_count" -eq 4 ] || { echo "H3_GPU_LIST must select exactly four unique GPUs" >&2; exit 1; }
  echo "config=pass"
}

preflight() {
  validate_config >/dev/null
  command -v nvidia-smi >/dev/null
  command -v python3 >/dev/null
  mkdir -p "$H3_ROOT"

  local available_kib required_kib
  available_kib="$(df -Pk "$H3_ROOT" | awk 'NR == 2 {print $4}')"
  required_kib="$((H3_REF2VA_BYTES / 1024 + (H3_DEPENDENCY_HEADROOM_GIB + H3_MIN_FREE_GIB) * 1024 * 1024))"
  [ "$available_kib" -ge "$required_kib" ] || {
    echo "insufficient disk: need checkpoint + dependencies + ${H3_MIN_FREE_GIB} GiB reserve" >&2
    exit 1
  }

  local gpu sample line used util
  IFS=',' read -r -a gpus <<<"$H3_GPU_LIST"
  for sample in 1 2 3; do
    for gpu in "${gpus[@]}"; do
      line="$(nvidia-smi -i "$gpu" --query-gpu=memory.used,utilization.gpu --format=csv,noheader,nounits)"
      used="${line%%,*}"; used="${used//[[:space:]]/}"
      util="${line##*,}"; util="${util//[[:space:]]/}"
      [ "$used" -lt 2048 ] && [ "$util" -lt 10 ] || {
        echo "GPU ${gpu} is busy: ${used} MiB, ${util}%" >&2
        exit 1
      }
    done
    [ "$sample" -eq 3 ] || sleep 2
  done

  if ss -ltn 2>/dev/null | awk -v port=":$H3_PORT" '$4 ~ port "$" {found=1} END {exit !found}'; then
    echo "port ${H3_PORT} is already in use" >&2
    exit 1
  fi
  echo "preflight=pass"
}

install_runtime() {
  preflight
  if [ -n "$H3_UV" ] && [ -x "$H3_UV" ]; then
    "$H3_UV" venv --python 3.12 "$H3_VENV"
    "$H3_UV" pip install --python "$H3_VENV/bin/python" \
      "sglang[diffusion]==$H3_SGLANG_VERSION" \
      --extra-index-url https://sgl-project.github.io/whl/cu129/ \
      --extra-index-url https://download.pytorch.org/whl/cu129 \
      --index-strategy unsafe-best-match
  elif python3 -c 'import ensurepip' >/dev/null 2>&1; then
    python3 -m venv "$H3_VENV"
    "$H3_VENV/bin/python" -m pip install --upgrade pip
    "$H3_VENV/bin/python" -m pip install "sglang[diffusion]==$H3_SGLANG_VERSION"
  elif command -v virtualenv >/dev/null; then
    virtualenv --clear "$H3_VENV"
    "$H3_VENV/bin/python" -m pip install --upgrade pip
    "$H3_VENV/bin/python" -m pip install "sglang[diffusion]==$H3_SGLANG_VERSION"
  else
    echo "python venv support or virtualenv is required" >&2
    exit 1
  fi
  "$H3_VENV/bin/python" -m pip freeze >"$H3_ROOT/requirements.lock"
  "$H3_VENV/bin/sglang" --help >/dev/null
  echo "install=pass"
}

launch() {
  preflight
  [ -x "$H3_VENV/bin/sglang" ] || { echo "run install first" >&2; exit 1; }
  mkdir -p "$H3_ROOT"/{cache/modelscope,cache/huggingface,logs,media}
  if [ -f "$H3_ROOT/${H3_MODEL_VARIANT}.pid" ] && kill -0 "$(cat "$H3_ROOT/${H3_MODEL_VARIANT}.pid")" 2>/dev/null; then
    echo "server already running" >&2
    exit 1
  fi
  CUDA_VISIBLE_DEVICES="$H3_GPU_LIST" \
  SGLANG_USE_MODELSCOPE=true \
  MODELSCOPE_CACHE="$H3_ROOT/cache/modelscope" \
  HF_HOME="$H3_ROOT/cache/huggingface" \
  nohup "$H3_VENV/bin/sglang" serve \
    --model-path "$H3_MODEL_ID" \
    --model-variant "$H3_MODEL_VARIANT" \
    --num-gpus 4 \
    --ulysses-degree 4 \
    --performance-mode speed \
    --enable-torch-compile false \
    --host "$H3_HOST" \
    --port "$H3_PORT" \
    >"$H3_ROOT/logs/${H3_MODEL_VARIANT}.log" 2>&1 &
  echo "$!" >"$H3_ROOT/${H3_MODEL_VARIANT}.pid"
  echo "launch=started pid=$!"
}

health() {
  local pid=""
  [ -f "$H3_ROOT/${H3_MODEL_VARIANT}.pid" ] && pid="$(cat "$H3_ROOT/${H3_MODEL_VARIANT}.pid")"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || { echo "health=stopped"; exit 1; }
  if curl -fsS --max-time 5 "http://$H3_HOST:$H3_PORT/health" >/dev/null; then
    echo "health=ready pid=$pid"
  else
    echo "health=starting pid=$pid"
    exit 2
  fi
}

stop_server() {
  local pid=""
  [ -f "$H3_ROOT/${H3_MODEL_VARIANT}.pid" ] && pid="$(cat "$H3_ROOT/${H3_MODEL_VARIANT}.pid")"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || { echo "stop=already-stopped"; return; }
  local cmdline
  cmdline="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
  case "$cmdline" in
    *sglang*"--model-variant $H3_MODEL_VARIANT"*"--port $H3_PORT"*) ;;
    *) echo "refusing to stop unverified pid $pid" >&2; exit 1 ;;
  esac
  kill "$pid"
  echo "stop=requested pid=$pid"
}

case "${1:-}" in
  print-config) print_config ;;
  validate-config) validate_config ;;
  preflight) preflight ;;
  install) install_runtime ;;
  launch) launch ;;
  health) health ;;
  stop) stop_server ;;
  *) echo "usage: $0 {print-config|validate-config|preflight|install|launch|health|stop}" >&2; exit 2 ;;
esac
