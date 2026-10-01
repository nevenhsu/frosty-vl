# Viggle Turbo v0.2.1

`viggle_turbo.py` is unchanged from
[Viggle/Qwen-Image-2.1-viggle-turbo](https://huggingface.co/Viggle/Qwen-Image-2.1-viggle-turbo/blob/bb26a0f38e5fe6c124aaccc9187a87eed5d9ed13/comfyui/viggle_turbo.py).

- Revision: `bb26a0f38e5fe6c124aaccc9187a87eed5d9ed13`
- SHA-256: `017911bb7d9c855c6ea6854adeeeea1aed576ca1c9cb487376419f5bae14e93d`
- Size: 5469 bytes
- The upstream LICENSE and NOTICE are included.

Frosty's `__init__.py` gives the nodes distinct names and exposes editable steps
with a default of six. `schedules.py` keeps the upstream 5/6/7 examples and
generates raw nodes for other counts, whose quality is unvalidated. Counts of
eight or more subdivide the high-noise region and preserve the four tail anchors;
one to four resample the six-step nodes. Resolution-dependent sigma shifting and
unmerged LoRA math remain upstream code. `runtime.py` registers hooks inside the
cleanup scope so partial installation failures cannot leak hooks onto the shared
model. GGUF/MPS compatibility and memory
capacity require a real generation test; upstream measurements are not Mac benchmarks.

`references.py` loads ordered references with ComfyUI's file validation and
uses the encoder capacity of sixteen references (fifteen plus a mask).
