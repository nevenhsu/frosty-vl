#!/usr/bin/env bash
set -euo pipefail

bundle_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
config_path=${FVL_COMFY_IMAGE_CONFIG:-$bundle_dir/config/comfyui-image.json}
python_bin=${FVL_COMFY_PYTHON:-$bundle_dir/.venv-comfy/bin/python}

if [[ ! -f "$config_path" ]]; then
  echo "Missing ComfyUI adapter config: $config_path" >&2
  echo "Copy config/comfyui-image.example.json to config/comfyui-image.json, then add your API workflow paths and node bindings." >&2
  exit 1
fi
if [[ ! -x "$python_bin" ]]; then
  echo "Missing ComfyUI adapter Python: $python_bin" >&2
  echo "Run $bundle_dir/start-qwen21-image.command to create the adapter environment." >&2
  exit 1
fi

export FVL_COMFY_IMAGE_CONFIG=$config_path
export FVL_ENGINES_FILE=${FVL_ENGINES_FILE:-$bundle_dir/config/comfyui-image-engines.json}
export FVL_UI_HOST=${FVL_UI_HOST:-127.0.0.1}
export FVL_UI_PORT=${FVL_UI_PORT:-8890}

cd "$bundle_dir"
"$python_bin" -m uvicorn server.comfy_image_serve:app --host 127.0.0.1 --port 8899 --workers 1 &
adapter_pid=$!
studio_pid=""
cleanup() {
  trap - EXIT INT TERM
  if [[ -n "$studio_pid" ]]; then
    kill "$studio_pid" 2>/dev/null || true
    wait "$studio_pid" 2>/dev/null || true
  fi
  kill "$adapter_pid" 2>/dev/null || true
  wait "$adapter_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

"$python_bin" "$bundle_dir/ui/webui.py" &
studio_pid=$!
while kill -0 "$adapter_pid" 2>/dev/null && kill -0 "$studio_pid" 2>/dev/null; do
  sleep 1
done
if ! kill -0 "$adapter_pid" 2>/dev/null; then
  status=0
  wait "$adapter_pid" || status=$?
  echo "Frosty adapter stopped; stopping Studio." >&2
  cleanup
  exit "$status"
fi
status=0
wait "$studio_pid" || status=$?
echo "Frosty Studio stopped; stopping the adapter." >&2
cleanup
exit "$status"
