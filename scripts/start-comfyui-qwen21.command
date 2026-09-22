#!/usr/bin/env bash
set -euo pipefail

install_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
comfyui_dir=${FVL_COMFYUI_DIR:-$install_root/ComfyUI}
model_dir=${FVL_QWEN21_MODEL_DIR:-$install_root/qwen21-downloads/models}
python_bin=$comfyui_dir/.venv/bin/python
config_path=${FVL_COMFYUI_MODEL_CONFIG:-$install_root/qwen21-downloads/comfyui_model_paths.yaml}

[[ -x "$python_bin" ]] || { echo "Missing ComfyUI environment: $python_bin" >&2; exit 1; }
[[ -f "$comfyui_dir/main.py" ]] || { echo "Missing ComfyUI main.py: $comfyui_dir/main.py" >&2; exit 1; }
mkdir -p "$(dirname "$config_path")"
{
  echo "qwen21_local:"
  printf '  base_path: "%s"\n' "${model_dir//\"/\\\"}"
  echo "  diffusion_models: diffusion_models"
  echo "  text_encoders: text_encoders"
  echo "  vae: vae"
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
