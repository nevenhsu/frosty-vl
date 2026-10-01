#!/usr/bin/env bash
# Portable Qwen Image 2.1 setup for an unpacked Frosty Studio folder.
# Downloads are pinned, resumable, and verified before their final names are used.
set -euo pipefail
umask 022

bundle_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
install_root=${FVL_QWEN21_HOME:-$(dirname "$bundle_dir")}
comfyui_dir=${FVL_COMFYUI_DIR:-$install_root/ComfyUI}
download_dir=$install_root/qwen21-downloads
model_dir=${FVL_QWEN21_MODEL_DIR:-${FVL_MODEL_DIR:-$download_dir/models}}
adapter_venv=$bundle_dir/.venv-comfy
comfyui_launcher=$install_root/start-comfyui-qwen21.command
check_only=0
profile=${FVL_QWEN21_PROFILE:-turbo}
cli_profile=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check-only) check_only=1 ;;
    --turbo|--base)
      requested_profile=${1#--}
      if [[ -n "$cli_profile" && "$cli_profile" != "$requested_profile" ]]; then
        echo "Choose only one profile: --base or --turbo." >&2
        exit 2
      fi
      cli_profile=$requested_profile
      ;;
    -h|--help)
      echo "Usage: /bin/bash setup-qwen21-macos.command [--base|--turbo] [--check-only]"
      echo "Profile: ${cli_profile:-$profile} (FVL_QWEN21_PROFILE may set the default)"
      echo "Install root: $install_root"
      echo "ComfyUI: $comfyui_dir"
      echo "Models: $model_dir"
      echo "Complete models and an existing ComfyUI installation are verified and skipped."
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
[[ -z "$cli_profile" ]] || profile=$cli_profile

fail() {
  echo >&2
  echo "[Stopped] $*" >&2
  exit 1
}

[[ $(uname -s) == Darwin ]] || fail "This setup supports macOS only."
[[ "$profile" == base || "$profile" == turbo ]] || fail "FVL_QWEN21_PROFILE must be base or turbo."
[[ $(uname -m) == arm64 ]] || fail "Run in a native Apple Silicon terminal, not Rosetta."
[[ $(id -u) -ne 0 ]] || fail "Run as a normal user, without sudo."
for tool in curl git shasum stat df awk xcode-select xcrun cmp cp mv chmod mkdir mktemp diff; do
  command -v "$tool" >/dev/null 2>&1 || fail "Required tool not found: $tool"
done
xcode-select -p >/dev/null
xcrun --find clang >/dev/null

lock_dir=$download_dir/.setup-lock
cleanup() {
  local owner=""
  if [[ -f "$lock_dir/pid" ]]; then IFS= read -r owner <"$lock_dir/pid" || true; fi
  if [[ "$owner" == "$$" ]]; then
    rm -f "$lock_dir/pid"
    rmdir "$lock_dir" 2>/dev/null || true
  fi
}
if [[ $check_only -eq 0 ]]; then
  mkdir -p "$download_dir"
  if ! mkdir "$lock_dir" 2>/dev/null; then
    fail "Another setup is running, or a stale lock exists: $lock_dir"
  fi
  printf '%s\n' "$$" >"$lock_dir/pid"
  trap cleanup EXIT
fi
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
if [[ "$profile" == turbo ]]; then
  model_rel+=("loras/Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors")
  model_size+=(679604800)
  model_sha+=("bafb91d0047df3f9b8a5a850b0c967f051164314d8aad778dfa34d9c24ec345b")
  model_url+=("https://huggingface.co/Viggle/Qwen-Image-2.1-viggle-turbo/resolve/bb26a0f38e5fe6c124aaccc9187a87eed5d9ed13/Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors?download=true")
fi
model_count=${#model_rel[@]}

default_config=$bundle_dir/config/comfyui-image.json
default_engines=$bundle_dir/config/comfyui-image-engines.json
if [[ "$profile" == turbo ]]; then
  default_config=$bundle_dir/config/comfyui-image-turbo.json
  default_engines=$bundle_dir/config/comfyui-image-turbo-engines.json
fi
config_path=${FVL_COMFY_IMAGE_CONFIG:-$default_config}

file_size() { stat -L -f '%z' "$1"; }
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
  echo "[$((index + 1))/$model_count] ${model_rel[$index]##*/}"
  if [[ -f "$target" ]]; then
    actual_size=$(file_size "$target")
    if [[ "$actual_size" == "${model_size[$index]}" ]]; then
      echo "Size matches; verifying local SHA-256..."
      if sha_matches "$target" "${model_sha[$index]}"; then
        echo "Verified; skipping download."
        return 0
      fi
    fi
    [[ $check_only -eq 1 ]] && return 2
    fail "Existing model failed verification: $target. No replacement or download was performed; check the file before re-running setup."
  elif [[ -e "$target" || -L "$target" ]]; then
    [[ $check_only -eq 1 ]] && return 2
    fail "Existing model path is not a readable regular file: $target. It was left unchanged."
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
  available_kb=$(df -Pk "${target%/*}" | awk 'NR == 2 {print $4}')
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
for ((index=0; index<model_count; index++)); do
  verify_or_download_model "$index" || model_errors=1
done

check_turbo_node_bundle() {
  local source=$bundle_dir/comfyui/frosty_viggle_turbo_v021
  local installed=$comfyui_dir/custom_nodes/frosty_viggle_turbo_v021
  local name
  [[ -f "$source/__init__.py" && -f "$source/viggle_turbo.py" ]] || fail "Turbo node bundle source is incomplete: $source"
  for name in __init__.py runtime.py references.py schedules.py viggle_turbo.py LICENSE NOTICE UPSTREAM.md; do
    [[ -f "$source/$name" ]] || continue
    [[ -f "$installed/$name" ]] || fail "Turbo node bundle is missing: $installed/$name"
    cmp -s "$source/$name" "$installed/$name" || fail "Turbo node bundle identity mismatch: $installed/$name"
  done
}

if [[ $check_only -eq 1 ]]; then
  [[ $model_errors -eq 0 ]] || fail "One or more models are missing or unverified."
  [[ -x "$comfyui_dir/.venv/bin/python" ]] || fail "ComfyUI environment is missing: $comfyui_dir"
  [[ -f "$comfyui_dir/custom_nodes/ComfyUI-GGUF/__init__.py" ]] || fail "ComfyUI-GGUF is missing."
  if [[ "$profile" == turbo ]]; then
    check_turbo_node_bundle
  fi
  [[ -x "$adapter_venv/bin/python" ]] || fail "Frosty adapter environment is missing."
  "$adapter_venv/bin/python" -c 'import fastapi, PIL, uvicorn; from websockets.sync.client import connect'
  [[ -f "$config_path" ]] || fail "Profile adapter config is missing: $config_path"
  echo "All portable runtime components are present."
  exit 0
fi

find_python312() {
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
}

comfy_commit=b0f4b7b294ce482a2e071d9d762c133d38c7aa07
gguf_commit=f912d5e5c25921e41eae2c0131eeb4d350e7c165
if [[ -e "$comfyui_dir" || -L "$comfyui_dir" ]]; then
  [[ -f "$comfyui_dir/main.py" ]] || fail "Existing ComfyUI path has no main.py: $comfyui_dir. It was left unchanged."
  [[ -x "$comfyui_dir/.venv/bin/python" ]] || fail "Existing ComfyUI has no usable .venv/bin/python: $comfyui_dir. Its installation was left unchanged."
  echo "Existing ComfyUI installation found; reusing its source, environment and settings."
  comfy_existing=1
else
  echo "Installing ComfyUI into the package runtime..."
  git clone https://github.com/Comfy-Org/ComfyUI.git "$comfyui_dir"
  git -C "$comfyui_dir" checkout --detach "$comfy_commit"
  comfy_existing=0
fi

if [[ ! -x "$comfyui_dir/.venv/bin/python" ]]; then
  echo "Creating ComfyUI Python environment..."
  find_python312
  "$python312" -m venv "$comfyui_dir/.venv"
fi
comfy_python=$comfyui_dir/.venv/bin/python
if ! "$comfy_python" -c 'import torch, yaml, safetensors' >/dev/null 2>&1; then
  [[ $comfy_existing -eq 0 ]] || fail "Existing ComfyUI environment is missing required imports (torch, yaml, safetensors). Its dependencies were left unchanged."
  echo "Installing ComfyUI dependencies..."
  "$comfy_python" -m pip install --upgrade pip
  "$comfy_python" -m pip install -r "$comfyui_dir/requirements.txt" -r "$comfyui_dir/manager_requirements.txt"
fi

gguf_dir=$comfyui_dir/custom_nodes/ComfyUI-GGUF
if [[ -e "$gguf_dir" || -L "$gguf_dir" ]]; then
  [[ -f "$gguf_dir/__init__.py" ]] || fail "Existing ComfyUI-GGUF node is incomplete: $gguf_dir. It was left unchanged."
  echo "Existing ComfyUI-GGUF node found; skipping clone."
  gguf_existing=1
else
  echo "Installing ComfyUI-GGUF..."
  mkdir -p "$comfyui_dir/custom_nodes"
  git clone https://github.com/leejet/ComfyUI-GGUF.git "$gguf_dir"
  git -C "$gguf_dir" checkout --detach "$gguf_commit"
  gguf_existing=0
fi
if ! "$comfy_python" -c 'import gguf' >/dev/null 2>&1; then
  [[ $gguf_existing -eq 0 ]] || fail "Existing ComfyUI-GGUF dependencies are unavailable. Its environment was left unchanged."
  echo "Installing ComfyUI-GGUF dependencies..."
  "$comfy_python" -m pip install -r "$gguf_dir/requirements.txt"
fi
if [[ $comfy_existing -eq 0 ]]; then
  "$comfy_python" -m pip check
fi

if [[ ! -x "$adapter_venv/bin/python" ]]; then
  echo "Creating Frosty adapter environment..."
  "$comfy_python" -m venv "$adapter_venv"
fi
if ! "$adapter_venv/bin/python" -c 'import fastapi, PIL, uvicorn; from websockets.sync.client import connect' >/dev/null 2>&1; then
  echo "Installing Frosty adapter dependencies..."
  "$adapter_venv/bin/python" -m pip install -r "$bundle_dir/requirements-comfy.txt"
fi

install_turbo_node_bundle() {
  local source=$bundle_dir/comfyui/frosty_viggle_turbo_v021
  local custom_nodes=$comfyui_dir/custom_nodes
  local installed=$custom_nodes/frosty_viggle_turbo_v021
  local temp backup name bundle_current
  [[ -f "$source/__init__.py" && -f "$source/viggle_turbo.py" ]] || fail "Turbo node bundle source is incomplete: $source"
  mkdir -p "$custom_nodes"
  if [[ -d "$installed" && ! -L "$installed" ]]; then
    bundle_current=1
    for name in __init__.py runtime.py references.py schedules.py viggle_turbo.py LICENSE NOTICE UPSTREAM.md; do
      [[ -f "$source/$name" ]] || continue
      if [[ ! -f "$installed/$name" ]] || ! cmp -s "$source/$name" "$installed/$name"; then
        bundle_current=0
        break
      fi
    done
    if ((bundle_current == 1)); then
      echo "Existing Frosty Viggle Turbo node bundle is current; skipping copy."
      return 0
    fi
    mkdir -p "$download_dir/node-backups"
    backup=$download_dir/node-backups/frosty_viggle_turbo_v021.$(date '+%Y%m%d-%H%M%S').$$
    mv "$installed" "$backup"
    echo "Backed up previous Frosty Viggle Turbo node bundle: $backup"
  elif [[ -e "$installed" || -L "$installed" ]]; then
    mkdir -p "$download_dir/node-backups"
    backup=$download_dir/node-backups/frosty_viggle_turbo_v021.$(date '+%Y%m%d-%H%M%S').$$
    mv "$installed" "$backup"
    echo "Backed up previous Frosty Viggle Turbo node bundle: $backup"
  fi
  temp=$custom_nodes/.frosty_viggle_turbo_v021.$$
  mkdir "$temp"
  cp -R "$source"/. "$temp"/
  mv "$temp" "$installed"
}

if [[ "$profile" == turbo ]]; then
  install_turbo_node_bundle
fi

launcher_temp=$download_dir/.start-comfyui-qwen21.command.$$
cp "$bundle_dir/scripts/start-comfyui-qwen21.command" "$launcher_temp"
chmod 755 "$launcher_temp"
if [[ -f "$comfyui_launcher" ]] && ! cmp -s "$launcher_temp" "$comfyui_launcher"; then
  launcher_backup=$comfyui_launcher.backup.$(date '+%Y%m%d-%H%M%S')
  cp -p "$comfyui_launcher" "$launcher_backup"
  echo "Backed up previous ComfyUI launcher: $launcher_backup"
fi
if [[ -f "$comfyui_launcher" ]] && cmp -s "$launcher_temp" "$comfyui_launcher"; then
  rm -f "$launcher_temp"
  chmod 755 "$comfyui_launcher"
  echo "Existing Frosty ComfyUI launcher is current; skipping copy."
else
  mv -f "$launcher_temp" "$comfyui_launcher"
  chmod 755 "$comfyui_launcher"
fi

echo "Checking MPS and portable Frosty configuration..."
"$comfy_python" -c 'import torch; assert torch.backends.mps.is_available(), "MPS is unavailable"; x=torch.ones((2,2),device="mps",dtype=torch.float16); torch.mps.synchronize(); assert (x@x).isfinite().all()'
model_config=$(mktemp "$download_dir/.frosty-model-paths.XXXXXX")
trap 'rm -f "$model_config"; cleanup' EXIT
{
  echo "qwen21_local:"
  printf '  base_path: "%s"\n' "${model_dir//\"/\\\"}"
  echo "  diffusion_models: diffusion_models"
  echo "  text_encoders: text_encoders"
  echo "  vae: vae"
  echo "  loras: loras"
} >"$model_config"
(
  cd "$comfyui_dir"
  PYTORCH_ENABLE_MPS_FALLBACK=1 "$comfy_python" - "$model_config" "$profile" <<'PY'
import asyncio
import sys

model_config = sys.argv[1]
profile = sys.argv[2]
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
if profile == "turbo":
    required.update({"FrostyViggleTurboLora", "FrostyViggleTurboSigmas", "BasicGuider",
                     "RandomNoise", "KSamplerSelect", "SamplerCustomAdvanced", "FrostyLoadReferenceImages"})
missing = required - set(nodes.NODE_CLASS_MAPPINGS)
if missing:
    raise RuntimeError("Missing required ComfyUI nodes: " + ", ".join(sorted(missing)))
print("Required ComfyUI nodes: passed")
PY
)
FVL_COMFY_IMAGE_CONFIG="$config_path" PYTHONPATH="$bundle_dir" \
  "$adapter_venv/bin/python" -c 'from server.comfy_image_serve import load_config; c=load_config(); assert set(c.workflows)=={"t2i","edit","masked"}; assert set(c.supported_modes)=={"transparent","extract","masked","annotate"}'
/bin/bash -n "$bundle_dir/start-frosty-qwen21"
/bin/bash -n "$comfyui_launcher"
chmod 755 "$bundle_dir/start-frosty-qwen21" "$bundle_dir/scripts/start-comfy-image.sh"

echo
echo "Setup complete. No full image generation was run."
echo "From now on, open Qwen Image Studio with:"
if [[ "$profile" == turbo ]]; then
  echo "  \"$bundle_dir/start-frosty-qwen21\""
else
  echo "  \"$bundle_dir/start-frosty-qwen21\" --base"
fi
echo "ComfyUI-only launcher: $comfyui_launcher"
echo "Profile: $profile"
echo "Studio URL: http://127.0.0.1:8890/image"
echo "Generated images: $comfyui_dir/output/Qwen21"
