#!/usr/bin/env bash
# Portable Qwen Image 2.1 setup for an unpacked Frosty Studio folder.
# Downloads are pinned, resumable, and verified before their final names are used.
set -euo pipefail
umask 022

bundle_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
install_root=${FVL_QWEN21_HOME:-$(dirname "$bundle_dir")}
comfyui_dir=$install_root/ComfyUI
download_dir=$install_root/qwen21-downloads
model_dir=$download_dir/models
adapter_venv=$bundle_dir/.venv-comfy
comfyui_launcher=$install_root/start-comfyui-qwen21.command
check_only=0

case "${1:-}" in
  "") ;;
  --check-only) check_only=1 ;;
  -h|--help)
    echo "Usage: /bin/bash setup-qwen21-macos.command [--check-only]"
    echo "Install root: $install_root"
    echo "Complete models and an existing ComfyUI installation are verified and skipped."
    exit 0
    ;;
  *) echo "Unknown option: $1" >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { echo "Too many options." >&2; exit 2; }

fail() {
  echo >&2
  echo "[Stopped] $*" >&2
  exit 1
}

[[ $(uname -s) == Darwin ]] || fail "This setup supports macOS only."
[[ $(uname -m) == arm64 ]] || fail "Run in a native Apple Silicon terminal, not Rosetta."
[[ $(id -u) -ne 0 ]] || fail "Run as a normal user, without sudo."
for tool in curl git shasum stat df awk xcode-select xcrun cmp cp mv chmod mkdir mktemp; do
  command -v "$tool" >/dev/null 2>&1 || fail "Required tool not found: $tool"
done
xcode-select -p >/dev/null
xcrun --find clang >/dev/null

mkdir -p "$download_dir"
lock_dir=$download_dir/.setup-lock
if ! mkdir "$lock_dir" 2>/dev/null; then
  fail "Another setup is running, or a stale lock exists: $lock_dir"
fi
printf '%s\n' "$$" >"$lock_dir/pid"
cleanup() {
  local owner=""
  if [[ -f "$lock_dir/pid" ]]; then IFS= read -r owner <"$lock_dir/pid" || true; fi
  if [[ "$owner" == "$$" ]]; then
    rm -f "$lock_dir/pid"
    rmdir "$lock_dir" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'echo "Setup interrupted; completed files and .part downloads were preserved." >&2; exit 130' INT
trap 'echo "Setup stopped; completed files and .part downloads were preserved." >&2; exit 143' TERM HUP

model_rel=(
  "diffusion_models/qwen-image-2.1-Q4_K_M.gguf"
  "text_encoders/qwen3vl_8b_int8_convrot.safetensors"
  "vae/qwen_image_2.1_vae_bf16.safetensors"
)
model_size=(4604557984 9350798360 675509688)
model_sha=(
  "833439e91bc1152d28f37aa198c7f6f4218b7de95754c2f7a318a2422ab4b2f8"
  "8bfd0f6e12abf2d2d697ecc888e5e90b0d6741d6708f05799f53afa560452e8f"
  "bb21f7473051e1ac368515dd3f2e15cd44d7a11748ee8823e1ddca3e4876b7c9"
)
model_url=(
  "https://huggingface.co/abenzerps/Qwen-Image-2.1-GGUF/resolve/a1fb8196bd5b8b18222dcde8fe09a023c72fbd6a/qwen-image-2.1-Q4_K_M.gguf?download=true"
  "https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/e83187db03c1bf47495c2e283582761e7c26bcf9/text_encoders/qwen3vl_8b_int8_convrot.safetensors?download=true"
  "https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/8150226f50722886a275fa08e7b1fdf961732502/vae/qwen_image_2.1_vae_bf16.safetensors?download=true"
)

file_size() { stat -f '%z' "$1"; }
sha_matches() {
  local actual
  actual=$(shasum -a 256 <"$1") || return 1
  [[ ${actual%% *} == "$2" ]]
}
preserve_unverified() {
  local path=$1 saved
  [[ -e "$path" || -L "$path" ]] || return 0
  saved="$path.unverified.$(date '+%Y%m%d-%H%M%S').$$"
  mv -n "$path" "$saved" || fail "Could not preserve unverified file: $path"
  echo "Preserved unverified file: $saved"
}

verify_or_download_model() {
  local index=$1 target part actual_size available_kb required
  target=$model_dir/${model_rel[$index]}
  part=$target.part
  echo
  echo "[$((index + 1))/3] ${model_rel[$index]##*/}"
  if [[ -f "$target" && ! -L "$target" ]]; then
    actual_size=$(file_size "$target")
    if [[ "$actual_size" == "${model_size[$index]}" ]]; then
      echo "Size matches; verifying local SHA-256..."
      if sha_matches "$target" "${model_sha[$index]}"; then
        echo "Verified; skipping download."
        return 0
      fi
    fi
    [[ $check_only -eq 1 ]] && return 2
    preserve_unverified "$target"
  elif [[ -e "$target" || -L "$target" ]]; then
    [[ $check_only -eq 1 ]] && return 2
    preserve_unverified "$target"
  fi
  [[ $check_only -eq 0 ]] || return 2

  mkdir -p "${target%/*}"
  if [[ -e "$part" && ( -L "$part" || ! -f "$part" ) ]]; then
    preserve_unverified "$part"
  fi
  if [[ -f "$part" ]]; then
    actual_size=$(file_size "$part")
    if [[ "$actual_size" == "${model_size[$index]}" ]]; then
      echo "Partial file is complete; verifying SHA-256 before any network request..."
      if sha_matches "$part" "${model_sha[$index]}"; then
        mv -n "$part" "$target"
        echo "Verified model: $target"
        return 0
      fi
      preserve_unverified "$part"
    elif [[ "$actual_size" -gt "${model_size[$index]}" ]]; then
      preserve_unverified "$part"
    else
      echo "Resuming partial download at $actual_size / ${model_size[$index]} bytes."
    fi
  fi
  available_kb=$(df -Pk "$download_dir" | awk 'NR == 2 {print $4}')
  [[ "$available_kb" =~ ^[0-9]+$ ]] || fail "Could not determine free disk space."
  actual_size=0
  [[ -f "$part" ]] && actual_size=$(file_size "$part")
  required=$((${model_size[$index]} - actual_size + 1073741824))
  [[ $((available_kb * 1024)) -ge $required ]] || fail "Not enough free space for ${model_rel[$index]##*/} plus a 1 GiB buffer."

  echo "Downloading with live transfer speed and ETA:"
  curl -q --fail --location --show-error \
    --proto '=https' --proto-redir '=https' \
    --retry 5 --retry-delay 3 --connect-timeout 30 \
    --speed-limit 1024 --speed-time 120 \
    --continue-at - --output "$part" "${model_url[$index]}" \
    || fail "Download incomplete. Re-run setup to resume $part"
  actual_size=$(file_size "$part")
  [[ "$actual_size" == "${model_size[$index]}" ]] || fail "Downloaded size mismatch for $part: $actual_size"
  echo "Download finished; verifying SHA-256..."
  sha_matches "$part" "${model_sha[$index]}" || { preserve_unverified "$part"; fail "SHA-256 mismatch; the file was preserved."; }
  [[ ! -e "$target" ]] || fail "Target appeared during download: $target"
  mv -n "$part" "$target"
  echo "Verified model: $target"
}

echo "Frosty Studio portable Qwen Image 2.1 setup"
echo "Package: $bundle_dir"
echo "Install root: $install_root"
echo "ComfyUI: $comfyui_dir"
echo "Models:  $model_dir"
model_errors=0
for index in 0 1 2; do
  verify_or_download_model "$index" || model_errors=1
done
if [[ $check_only -eq 1 ]]; then
  [[ $model_errors -eq 0 ]] || fail "One or more models are missing or unverified."
  [[ -x "$comfyui_dir/.venv/bin/python" ]] || fail "ComfyUI environment is missing: $comfyui_dir"
  [[ -f "$comfyui_dir/custom_nodes/ComfyUI-GGUF/__init__.py" ]] || fail "ComfyUI-GGUF is missing."
  [[ -x "$adapter_venv/bin/python" ]] || fail "Frosty adapter environment is missing."
  "$adapter_venv/bin/python" -c 'import fastapi, PIL, uvicorn'
  echo "All portable runtime components are present."
  exit 0
fi

if [[ -x /opt/homebrew/opt/python@3.12/bin/python3.12 ]]; then
  python312=/opt/homebrew/opt/python@3.12/bin/python3.12
elif command -v python3.12 >/dev/null 2>&1; then
  python312=$(command -v python3.12)
else
  brew_bin=$(command -v brew || true)
  [[ -n "$brew_bin" ]] || fail "Python 3.12 is missing. Install Homebrew, then re-run setup."
  echo "Installing Python 3.12 with Homebrew..."
  "$brew_bin" install python@3.12
  python312=$($brew_bin --prefix python@3.12)/bin/python3.12
fi
"$python312" -c 'import platform; assert platform.machine() == "arm64"'

comfy_commit=b0f4b7b294ce482a2e071d9d762c133d38c7aa07
gguf_commit=f912d5e5c25921e41eae2c0131eeb4d350e7c165
if [[ -e "$comfyui_dir" || -L "$comfyui_dir" ]]; then
  [[ -d "$comfyui_dir/.git" && ! -L "$comfyui_dir" ]] || fail "Existing ComfyUI path is not a normal Git checkout: $comfyui_dir"
  echo "Existing ComfyUI checkout found; skipping clone."
else
  echo "Installing ComfyUI into the package runtime..."
  git clone https://github.com/Comfy-Org/ComfyUI.git "$comfyui_dir"
  git -C "$comfyui_dir" checkout --detach "$comfy_commit"
fi

if [[ ! -x "$comfyui_dir/.venv/bin/python" ]]; then
  echo "Creating ComfyUI Python environment..."
  "$python312" -m venv "$comfyui_dir/.venv"
fi
comfy_python=$comfyui_dir/.venv/bin/python
if ! "$comfy_python" -c 'import torch, yaml, safetensors' >/dev/null 2>&1; then
  echo "Installing ComfyUI dependencies..."
  "$comfy_python" -m pip install --upgrade pip
  "$comfy_python" -m pip install -r "$comfyui_dir/requirements.txt" -r "$comfyui_dir/manager_requirements.txt"
fi

gguf_dir=$comfyui_dir/custom_nodes/ComfyUI-GGUF
if [[ -e "$gguf_dir" || -L "$gguf_dir" ]]; then
  [[ -d "$gguf_dir/.git" && ! -L "$gguf_dir" ]] || fail "Existing ComfyUI-GGUF path is not a normal Git checkout: $gguf_dir"
  echo "Existing ComfyUI-GGUF node found; skipping clone."
else
  echo "Installing ComfyUI-GGUF..."
  mkdir -p "$comfyui_dir/custom_nodes"
  git clone https://github.com/leejet/ComfyUI-GGUF.git "$gguf_dir"
  git -C "$gguf_dir" checkout --detach "$gguf_commit"
fi
if ! "$comfy_python" -c 'import gguf' >/dev/null 2>&1; then
  echo "Installing ComfyUI-GGUF dependencies..."
  "$comfy_python" -m pip install -r "$gguf_dir/requirements.txt"
fi
"$comfy_python" -m pip check

if [[ ! -x "$adapter_venv/bin/python" ]]; then
  echo "Creating Frosty adapter environment..."
  "$python312" -m venv "$adapter_venv"
fi
if ! "$adapter_venv/bin/python" -c 'import fastapi, PIL, uvicorn' >/dev/null 2>&1; then
  echo "Installing Frosty adapter dependencies..."
  "$adapter_venv/bin/python" -m pip install -r "$bundle_dir/requirements-comfy.txt"
fi

launcher_temp=$download_dir/.start-comfyui-qwen21.command.$$
cp "$bundle_dir/scripts/start-comfyui-qwen21.command" "$launcher_temp"
chmod 755 "$launcher_temp"
if [[ -f "$comfyui_launcher" ]] && ! cmp -s "$launcher_temp" "$comfyui_launcher"; then
  launcher_backup=$comfyui_launcher.backup.$(date '+%Y%m%d-%H%M%S')
  cp -p "$comfyui_launcher" "$launcher_backup"
  echo "Backed up previous ComfyUI launcher: $launcher_backup"
fi
mv -f "$launcher_temp" "$comfyui_launcher"
chmod 755 "$comfyui_launcher"

echo "Checking MPS and portable Frosty configuration..."
"$comfy_python" -c 'import torch; assert torch.backends.mps.is_available(), "MPS is unavailable"; x=torch.ones((2,2),device="mps",dtype=torch.float16); torch.mps.synchronize(); assert (x@x).isfinite().all()'
model_config=$download_dir/comfyui_model_paths.yaml
{
  echo "qwen21_local:"
  printf '  base_path: "%s"\n' "${model_dir//\"/\\\"}"
  echo "  diffusion_models: diffusion_models"
  echo "  text_encoders: text_encoders"
  echo "  vae: vae"
} >"$model_config"
(
  cd "$comfyui_dir"
  PYTORCH_ENABLE_MPS_FALLBACK=1 "$comfy_python" - "$model_config" <<'PY'
import asyncio
import sys

model_config = sys.argv[1]
sys.argv = [
    "probe", "--extra-model-paths-config", model_config, "--disable-api-nodes",
    "--fp16-unet", "--cpu-vae", "--fp32-vae", "--cache-none",
    "--disable-smart-memory", "--preview-method", "none",
]
import comfy.options
comfy.options.enable_args_parsing()
from comfy.cli_args import args
import nodes

asyncio.run(nodes.init_extra_nodes(init_api_nodes=False))
required = {
    "UnetLoaderGGUF", "CLIPLoader", "VAELoader", "TextEncodeQwenImage21",
    "EmptyLatentImage", "LoadImage", "QwenImage21Cache", "KSampler",
    "VAEDecodeTiled", "PreviewImage",
}
missing = required - set(nodes.NODE_CLASS_MAPPINGS)
if missing:
    raise RuntimeError("Missing required ComfyUI nodes: " + ", ".join(sorted(missing)))
print("Required ComfyUI nodes: passed")
PY
)
FVL_COMFY_IMAGE_CONFIG="$bundle_dir/config/comfyui-image.json" PYTHONPATH="$bundle_dir" \
  "$adapter_venv/bin/python" -c 'from server.comfy_image_serve import load_config; c=load_config(); assert set(c.workflows)=={"t2i","edit"}'
/bin/bash -n "$bundle_dir/start-frosty-qwen21"
/bin/bash -n "$comfyui_launcher"

echo
echo "Setup complete. No full image generation was run."
echo "From now on, open Qwen Image Studio with:"
echo "  \"$bundle_dir/start-frosty-qwen21\""
echo "ComfyUI-only launcher: $comfyui_launcher"
echo "Studio URL: http://127.0.0.1:8890/image"
echo "Generated images: $comfyui_dir/output/Qwen21"
