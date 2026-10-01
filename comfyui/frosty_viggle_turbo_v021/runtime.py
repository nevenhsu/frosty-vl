"""Unmerged upstream LoRA math with cleanup covering hook installation too."""
import json

import torch.nn.functional as F

from . import viggle_turbo as upstream


def run_with_lora(lora, executor, *args, **kwargs):
    dm = executor.class_obj
    for ab in lora.values():
        if ab[0].device != args[0].device:
            ab[0], ab[1] = ab[0].to(args[0].device), ab[1].to(args[0].device)
    hooks = []
    try:
        for name, ab in lora.items():
            parent, _, leaf = name.rpartition(".")
            mlp = dm.get_submodule(parent)
            if not getattr(mlp, "fused", False):
                hooks.append(upstream.add_hook(dm.get_submodule(name), ab))
            elif leaf == "out":
                gate, up = lora[parent + ".gate_layer"], lora[parent + ".proj"]
                # Register each handle in the cleanup scope, including partial
                # fused-MLP installation. Each MLP owns its intermediate output.
                def install_fused(mlp, gate, up, down):
                    saved = {}

                    def gate_hook(module, inputs, output):
                        saved["gu"] = output + upstream.torch.cat([
                            upstream.lora_fwd(inputs[0], gate),
                            upstream.lora_fwd(inputs[0], up),
                        ], -1)
                        return saved["gu"]

                    def mlp_hook(module, inputs, output):
                        g, u = saved.pop("gu").chunk(2, -1)
                        return output + upstream.lora_fwd(F.silu(g) * u, down)

                    hooks.append(mlp.gate_up.register_forward_hook(gate_hook))
                    hooks.append(mlp.register_forward_hook(mlp_hook))

                install_fused(mlp, gate, up, ab)
        return executor(*args, **kwargs)
    finally:
        for hook in hooks:
            hook.remove()


class FrostyViggleTurboLora(upstream.ViggleTurboLora):
    def load(self, model, lora_name, strength):
        sd, meta = upstream.comfy.utils.load_torch_file(
            upstream.folder_paths.get_full_path_or_raise("loras", lora_name), return_metadata=True)
        cfg = json.loads((meta or {}).get("lora_adapter_metadata", "{}"))
        scale = strength * cfg.get("transformer.lora_alpha", 1) / cfg.get("transformer.r", 1)
        lora = {key.removeprefix("transformer.").removesuffix(".lora_A.weight"):
                [sd[key], sd[key.replace("lora_A", "lora_B")] * scale]
                for key in sd if key.endswith(".lora_A.weight")}
        result = model.clone()
        result.add_wrapper_with_key(
            upstream.comfy.patcher_extension.WrappersMP.DIFFUSION_MODEL, "frosty_viggle_turbo_lora",
            lambda executor, *args, **kwargs: run_with_lora(lora, executor, *args, **kwargs))
        return (result,)
