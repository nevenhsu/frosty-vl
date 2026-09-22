# Use Frosty Image with ComfyUI

This adapter keeps Frosty's Image workspace and gallery while an existing local
ComfyUI installation performs the render. It supports text-to-image, reference
editing, and four Qwen Image modes without loading Frosty's CUDA/Diffusers backend.

```text
Frosty Image :8890 -> Frosty ComfyUI adapter :8899 -> ComfyUI :8188
```

The adapter process itself does not install ComfyUI or download models. The
portable setup below installs those prerequisites and supplies API-format workflow
templates for text-to-image, reference editing, and masked editing; custom
workflows still need their own explicit node bindings.

## Portable Apple Silicon setup

Keep the three folders together. Their parent can be anywhere and can have any
name; the launchers resolve paths from their own locations.

```text
<install folder>/
├── start-comfyui-qwen21.command
├── ComfyUI/
├── frosty-vl/
│   ├── setup-qwen21-macos.command
│   └── start-frosty-qwen21
└── qwen21-downloads/
    └── models/
```

Run `frosty-vl/setup-qwen21-macos.command` once. It verifies complete model files
and skips their downloads, resumes `.part` files with curl's visible speed and
ETA, skips an existing ComfyUI/GGUF checkout, and creates the two Python
environments. Downloads are pinned and SHA-256 checked before their final names
are used.

After setup, drag `frosty-vl/start-frosty-qwen21` into Terminal and press Enter.
It starts ComfyUI, the adapter and Studio as needed, then opens
[http://127.0.0.1:8890/image](http://127.0.0.1:8890/image). Generated images,
metadata and recoverable Trash live only under `ComfyUI/output/Qwen21`; Frosty
does not keep a second image copy. The parent-level
`start-comfyui-qwen21.command` opens ComfyUI by itself for troubleshooting.

## 1. Export the workflows

In ComfyUI, export the working text-to-image, image-edit, and (when enabled)
masked-edit graphs in **API format**. A normal UI workflow is not interchangeable
with an API-format export. The repository includes
`workflows/qwen21_t2i_api.json`, `workflows/qwen21_edit_api.json`, and
`workflows/qwen21_masked_api.json` as Qwen Image 2.1 templates. Keep custom JSON
files outside the Git repository if they contain private paths or configuration.

The text-to-image and edit workflows are required. The masked workflow is
optional, but it must have separate `LoadImage` nodes for the original image and
the mask. Do not bind a filename list to one `LoadImage` node: the masked graph
needs one binding for the reference image and another for the mask image.

Record the node ID and input name for each value Frosty should control:

- positive prompt;
- negative prompt;
- seed, steps, width, height and guidance/CFG;
- the `LoadImage`-style input used by the edit workflow;
- for masked editing, separate `LoadImage` inputs for the reference and mask.

Node IDs belong to the exported workflow and can differ from every example. The
adapter validates every binding and stops with a clear configuration error when a
node or input does not exist.

## 2. Create the adapter environment

From this checkout:

```bash
python3 -m venv .venv-comfy
source .venv-comfy/bin/activate
python -m pip install -r requirements-comfy.txt
```

This environment is only for the adapter and Studio. Do not merge the CUDA Image
requirements into a working ComfyUI environment.

Copy the example configuration:

```bash
cp config/comfyui-image.example.json config/comfyui-image.json
```

Edit `config/comfyui-image.json` so `workflows.t2i.path` and
`workflows.edit.path` point to the required API exports. If masked editing is
enabled, set `workflows.masked.path` to its API export as well. Replace every
example node ID and input name under `bindings` with the values from those
exports. Relative paths are resolved from the configuration file's folder.

The top-level `supported_modes` list advertises the four modes:
`transparent`, `extract`, `masked`, and `annotate`. Leave `masked` out of a
custom configuration when no masked workflow is available; the bundled
configuration includes it.

The edit example binds one reference image. The bundled masked configuration
binds the original image to `460.image` and the mask to `475.image` as separate
inputs. Add an ordered list of explicit node/input bindings only when the edit
workflow actually accepts multiple images; Frosty will otherwise present a
one-image editing interface.

## 3. Start Frosty

Start ComfyUI on loopback port `8188`, activate `.venv-comfy`, then run:

```bash
./scripts/start-comfy-image.sh
```

Open [http://127.0.0.1:8890/image](http://127.0.0.1:8890/image). The launcher keeps
the raw ComfyUI and adapter APIs on loopback. Stop it with `Ctrl-C`; ComfyUI remains
owned by its existing launcher.

To keep the private config elsewhere, set its absolute path first:

```bash
FVL_COMFY_IMAGE_CONFIG=/absolute/path/comfyui-image.json ./scripts/start-comfy-image.sh
```

## What is translated

Frosty selects the text-to-image workflow when no reference is attached and the
edit workflow when a reference is present. The adapter uploads ordered references,
copies the workflow template for each job, injects the bound prompt and controls,
queues it through ComfyUI, monitors history, downloads finished images, and saves
PNG files plus metadata in Frosty's recoverable gallery.

The bundled Qwen Image 2.1 edit and masked workflows derive their canvas from the
reference and use detail resolution `384`. While a reference is attached, Studio
disables the text-to-image size controls and labels this fixed edit behavior
explicitly.

The four advertised modes are translated as follows:

- **Transparent:** use text-to-image with no reference, or the edit workflow with
  a reference, to request a transparent result.
- **Extract:** requires a reference and reuses the edit workflow. Extraction is
  generative and therefore non-deterministic; it is not deterministic
  segmentation.
- **Masked:** requires one reference and one mask and uses the dedicated masked
  workflow. White mask areas are the regions to modify. When “Keep pixels outside
  the mask unchanged” is enabled, the adapter performs an exact composite over the
  original so pixels outside the selected region are preserved.
- **Annotate:** requires a reference and reuses the edit workflow for the
  requested annotation change.

Frosty hides unavailable modes based on adapter capabilities and configuration.

## Troubleshooting

- **Adapter says a node/input is missing:** export the current API workflow again
  and update the matching binding. Do not copy node IDs from the example.
- **ComfyUI is unreachable:** confirm its own UI works at `127.0.0.1:8188` and that
  `comfyui_url` matches its private listener.
- **Workflow completes without an image:** ensure the API workflow has an image
  output recorded in ComfyUI history.
- **A control is not bound:** add an explicit binding rather than baking a guessed
  node into the adapter. Requested non-default values are rejected when they cannot
  be applied safely.

Source/config validation and fixture tests do not prove the user's model workflow.
This documentation does not claim a real GPU generation. Completion requires
small consumer-path checks through the browser after the workflow files and model
runtime are supplied, including a masked job when that optional workflow is
enabled.
