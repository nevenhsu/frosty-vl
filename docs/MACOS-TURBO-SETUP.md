# Qwen Image 2.1 Turbo on Apple Silicon

This guide adds the pinned Viggle Qwen Image 2.1 v0.2.1 Turbo LoRA to the
portable ComfyUI setup. It is intended for an existing Apple Silicon install,
including the selected M1 Pro 16 GB machine. The setup keeps the base Qwen
Image 2.1 files and the Turbo LoRA in the same model directory:

```text
<install root>/qwen21-downloads/models/
├── diffusion_models/qwen-image-2.1-Q4_K_M.gguf
├── text_encoders/qwen3vl_8b_int8_convrot.safetensors
├── vae/qwen_image_2.1_vae_bf16.safetensors
└── loras/Qwen-Image-2.1-viggle-turbo-v0.2.1-6step-lora-r128.safetensors
```

The LoRA is downloaded from the pinned Hugging Face revision
`bb26a0f38e5fe6c124aaccc9187a87eed5d9ed13`, verified at 679,604,800 bytes with
SHA-256 `bafb91d0047df3f9b8a5a850b0c967f051164314d8aad778dfa34d9c24ec345b`,
and resumed through its `.part` file when interrupted. The three base files
retain their existing filenames and checksums. Model files are not included in
the repository.

## Install or verify

Run the base setup when you only want the existing three-model profile:

```bash
/bin/bash ./setup-qwen21-macos.command --base
```

Turbo is the default setup profile. Run it to install the workflow, node bundle, and LoRA:

```bash
/bin/bash ./setup-qwen21-macos.command
```

After setup, the checks below do not install packages, download files, or start
services:

```bash
/bin/bash ./setup-qwen21-macos.command --base --check-only
/bin/bash ./setup-qwen21-macos.command --turbo --check-only
```

Turbo check-only mode verifies all three base files, the LoRA, ComfyUI-GGUF,
the versioned `frosty_viggle_turbo_v021` bundle, the adapter environment, and
the selected adapter config. It does not create the download directory or a
setup lock.

## Start a profile

Turbo is the default and opens Frosty Image Studio:

```bash
./start-frosty-qwen21
```

Return to the original workflow with:

```bash
./start-frosty-qwen21 --base
```

The equivalent direct adapter command is `./scripts/start-comfy-image.sh`.
All four setup/start scripts default to Turbo. Set `FVL_QWEN21_PROFILE=base`
for a base default; an explicit `--base` or `--turbo` flag takes precedence. The
launchers select `config/comfyui-image.json` and
`config/comfyui-image-engines.json` for base, and the corresponding
`comfyui-image-turbo` files for Turbo. Set `FVL_COMFY_IMAGE_CONFIG` or
`FVL_ENGINES_FILE` to use a private config explicitly.

Every Image workspace uses the selected Turbo profile: create/edit automatically,
transparent images, subject extraction, mask editing, and annotation editing.
They route through the Turbo text-to-image, edit, or masked API graph.

The raw ComfyUI API and Frosty adapter stay on loopback (`8188` and `8899`);
Studio is at `http://127.0.0.1:8890/image`. Existing processes are inspected
for profile compatibility and are never silently replaced. A running adapter
with a different health `profile` stops startup with an actionable error.

## Paths and capacity

The default install root is the parent of this checkout. Override it and the
two runtime directories when needed:

```bash
FVL_QWEN21_HOME=/Volumes/Models/frosty \
FVL_COMFYUI_DIR=/Volumes/Models/frosty/ComfyUI \
FVL_QWEN21_MODEL_DIR=/Volumes/Models/frosty/qwen21-downloads/models \
  ./start-frosty-qwen21 --turbo
```

`FVL_MODEL_DIR` is accepted as an alias for the model directory for scripted
environments. The setup forwards the selected ComfyUI and model paths to the
copied launcher and writes `loras: loras` to every generated extra-model-paths
YAML file.

Turbo opens at 512 × 512 for text-to-image; edit workflows use reference-derived
aspect ratios with a default detail resolution of 512. Turbo uses six sampling
steps by default. Steps accept positive integer counts without an application ceiling or whitelist.
The default and recommendation remain six steps.
The installed ComfyUI encoder has 16 image slots: edit accepts 16 references,
and masked edit accepts 15 references plus a mask. The Turbo model is primarily
trained/evaluated for 1–3 references; 4–16 describes wiring capacity, not a quality guarantee.
CFG is fixed at `1`; negative prompts are disabled for the Turbo sampler. The selected
M1 Pro 16 GB target is supported as an installation target, but 16 GB is not a
qualified VRAM fit guarantee for every resolution or reference workload. This
guide makes no five-times speed promise; verify a small consumer-path image on
the target machine before relying on Turbo for a workload.

## Rerun on another Mac

For a Mac that already has the three verified Qwen Image 2.1 files and a
ComfyUI installation, use these two commands from the checkout:

```bash
/bin/bash ./setup-qwen21-macos.command
./start-frosty-qwen21
```

The default Turbo rerun verifies the existing model files and reuses a
compatible ComfyUI source, `.venv`, settings, and model-path configuration. If
the base files are already present, the only model download is the missing
Turbo LoRA. Existing model files that fail size or SHA-256 verification stop the
rerun before any replacement or network request; inspect the file and rerun
after correcting it. A valid model symlink is verified through its target.

Compatibility is based on the installed ComfyUI source and its actual Qwen
Image 2.1 node preflight, including the GGUF loader and the Turbo nodes. An
older or incompatible runtime stops with its files unchanged. Setup does not
force a checkout, reset an existing ComfyUI tree, recreate its environment, or
promise compatibility with every ComfyUI version.

The Turbo edit workflows use the encoder capacity of sixteen ordered references
(fifteen plus one mask for masked editing). The loader uses ComfyUI file validation
and sends each image to its own Qwen image slot. Viggle trained on one to three
references; quality with more references is unmeasured. Capacity does not establish
a memory fit or edit-quality guarantee.

The exact 5-, 6-, and 7-step raw schedules are upstream examples. Other counts
are generated by this integration: eight or more subdivide the high-noise region
while retaining the four low-noise anchors; one to four resample the six-step
schedule. Their quality is unvalidated, and more steps do not guarantee a better
image. Nine steps here use ordinary Turbo sampling; this is not the separate
base-model-tail mode introduced with upstream v0.3. The installed weights remain
the pinned v0.2.1 adapter. See [upstream sampling guidance](https://huggingface.co/Viggle/Qwen-Image-2.1-viggle-turbo#rules-that-matter).

Completed photos save per-image generation `seconds` in their PNG JSON sidecar.
Gallery shows this value after size and seed. The first variation includes model
loading and input preparation; later variations record their own elapsed time.
Existing photos without recorded seconds omit this field in Gallery.

Studio subscribes to ComfyUI execution events for the current image job. Loading,
encoding and decoding show an animated waiting bar; sampler callbacks show the
actual step count and percentage. If live events are unavailable, rendering still
finishes through history polling, without inventing a step count.
Cancel removes the queued prompt or sends a targeted interrupt for the running
prompt. Studio keeps showing Cancelling until ComfyUI confirms that prompt is no
longer queued or running. An active tensor operation may still need to reach its
next interrupt checkpoint. The verified targeted API requires ComfyUI 0.37 or
newer; an older or unrecognized server never receives a global interrupt.

## Local verification

On 2026-10-01, the M1 Pro 16 GB installation completed a 512 × 512 six-step
text-to-image job through Studio in about 157 seconds, including model loading.
A three-reference edit (the same generated test image supplied three times)
completed at the earlier 384-detail default in about 235 seconds. After relaxing
steps and setting the UI default to 512, launching `./start-frosty-qwen21` without
flags completed a 512 × 512 seven-step job in about 173 seconds. Saved PNGs,
sidecars, Studio downloads, and private listeners were checked.

Every Image workspace routes to a Turbo graph and has a regression check.
Transparent output, subject extraction, masked editing, and annotation editing
have not each received a real model-quality test. Ten-reference loading and graph
validation pass; only three references have been exercised in a real edit.
The 16 GB runs used swap, so these results do not qualify larger workloads.
