#!/usr/bin/env bash
set -euo pipefail

bundle_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
profile=${FVL_QWEN21_PROFILE:-turbo}
cli_profile=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --turbo|--base)
      requested_profile=${1#--}
      if [[ -n "$cli_profile" && "$cli_profile" != "$requested_profile" ]]; then
        echo "Choose only one profile: --base or --turbo." >&2
        exit 2
      fi
      cli_profile=$requested_profile
      ;;
    -h|--help)
      echo "Usage: $0 [--base|--turbo]"
      echo "Profile: ${cli_profile:-$profile} (FVL_QWEN21_PROFILE may set the default)"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
[[ -z "$cli_profile" ]] || profile=$cli_profile
if [[ "$profile" != base && "$profile" != turbo ]]; then
  echo "FVL_QWEN21_PROFILE must be base or turbo." >&2
  exit 1
fi

default_config=$bundle_dir/config/comfyui-image.json
default_engines=$bundle_dir/config/comfyui-image-engines.json
if [[ "$profile" == turbo ]]; then
  default_config=$bundle_dir/config/comfyui-image-turbo.json
  default_engines=$bundle_dir/config/comfyui-image-turbo-engines.json
fi
config_path=${FVL_COMFY_IMAGE_CONFIG:-$default_config}
if [[ -z "${FVL_COMFY_IMAGE_OUTPUT_DIR:-}" && -z "${FVL_COMFY_IMAGE_CONFIG:-}" ]]; then
  install_root=${FVL_QWEN21_HOME:-$(dirname "$bundle_dir")}
  comfyui_dir=${FVL_COMFYUI_DIR:-$install_root/ComfyUI}
  export FVL_COMFY_IMAGE_OUTPUT_DIR=$comfyui_dir/output/Qwen21
fi
python_bin=${FVL_COMFY_PYTHON:-$bundle_dir/.venv-comfy/bin/python}

if [[ ! -f "$config_path" ]]; then
  echo "Missing ComfyUI adapter config: $config_path" >&2
  echo "Copy config/comfyui-image.example.json to config/comfyui-image.json, then add your API workflow paths and node bindings." >&2
  exit 1
fi
if [[ ! -x "$python_bin" ]]; then
  echo "Missing ComfyUI adapter Python: $python_bin" >&2
  echo "Run $bundle_dir/setup-qwen21-macos.command to create the adapter environment." >&2
  exit 1
fi

export FVL_COMFY_IMAGE_CONFIG=$config_path
export FVL_QWEN21_PROFILE=$profile
export FVL_ENGINES_FILE=${FVL_ENGINES_FILE:-$default_engines}
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
