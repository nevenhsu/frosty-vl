#!/usr/bin/env bash
set -euo pipefail

launcher_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [[ "$(basename "$launcher_dir")" == scripts ]]; then
  bundle_dir=$(cd "$launcher_dir/.." && pwd)
  default_install_root=$(dirname "$bundle_dir")
else
  default_install_root=$launcher_dir
fi
install_root=${FVL_QWEN21_HOME:-$default_install_root}
comfyui_dir=${FVL_COMFYUI_DIR:-$install_root/ComfyUI}
model_dir=${FVL_QWEN21_MODEL_DIR:-${FVL_MODEL_DIR:-$install_root/qwen21-downloads/models}}
python_bin=$comfyui_dir/.venv/bin/python
config_path=${FVL_COMFYUI_MODEL_CONFIG:-$install_root/qwen21-downloads/comfyui_model_paths.yaml}
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
      echo "ComfyUI: $comfyui_dir"
      echo "Models: $model_dir"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
[[ -z "$cli_profile" ]] || profile=$cli_profile
[[ "$profile" == base || "$profile" == turbo ]] || { echo "FVL_QWEN21_PROFILE must be base or turbo." >&2; exit 1; }

[[ -x "$python_bin" ]] || { echo "Missing ComfyUI environment: $python_bin" >&2; exit 1; }
[[ -f "$comfyui_dir/main.py" ]] || { echo "Missing ComfyUI main.py: $comfyui_dir/main.py" >&2; exit 1; }
if [[ "$profile" == turbo ]]; then
  [[ -f "$comfyui_dir/custom_nodes/frosty_viggle_turbo_v021/__init__.py" ]] || { echo "Missing Turbo node bundle: $comfyui_dir/custom_nodes/frosty_viggle_turbo_v021" >&2; exit 1; }
  [[ -f "$model_dir/loras/Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors" ]] || { echo "Missing Turbo LoRA: $model_dir/loras/Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors" >&2; exit 1; }
fi
mkdir -p "$(dirname "$config_path")"
{
  echo "qwen21_local:"
  printf '  base_path: "%s"\n' "${model_dir//\"/\\\"}"
  echo "  diffusion_models: diffusion_models"
  echo "  text_encoders: text_encoders"
  echo "  vae: vae"
  echo "  loras: loras"
} >"$config_path"

if [[ ${FVL_NO_BROWSER:-0} != 1 ]]; then
  (
    for _ in {1..180}; do
      if curl -fsS --max-time 2 http://127.0.0.1:8188/system_stats >/dev/null 2>&1; then
        open http://127.0.0.1:8188
        exit 0
      fi
      sleep 1
    done
  ) &
fi

cd "$comfyui_dir"
unset PYTHONHOME PYTHONPATH PYTORCH_MPS_HIGH_WATERMARK_RATIO PYTORCH_MPS_LOW_WATERMARK_RATIO
export PYTHONNOUSERSITE=1 PYTHONUNBUFFERED=1 PYTHONFAULTHANDLER=1 PYTORCH_ENABLE_MPS_FALLBACK=1
exec /usr/bin/caffeinate -i "$python_bin" -u main.py \
  --listen 127.0.0.1 \
  --port 8188 \
  --extra-model-paths-config "$config_path" \
  --enable-manager \
  --disable-api-nodes \
  --fp16-unet \
  --cpu-vae \
  --fp32-vae \
  --cache-none \
  --disable-smart-memory \
  --preview-method none
