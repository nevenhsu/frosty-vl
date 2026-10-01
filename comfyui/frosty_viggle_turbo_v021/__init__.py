"""Frosty controls around the pinned, unmodified Viggle v0.2.1 nodes."""
from .viggle_turbo import ViggleTurboSigmas
from .runtime import FrostyViggleTurboLora
from .references import FrostyLoadReferenceImages
from .schedules import raw_nodes


class FrostyViggleTurboSigmas(ViggleTurboSigmas):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "latent": ("LATENT",),
            "steps": ("INT", {"default": 6, "min": 1, "step": 1}),
        }}

    def get_sigmas(self, latent, steps):
        return super().get_sigmas(latent, ",".join(map(str, raw_nodes(steps))))


NODE_CLASS_MAPPINGS = {
    "FrostyViggleTurboLora": FrostyViggleTurboLora,
    "FrostyViggleTurboSigmas": FrostyViggleTurboSigmas,
    "FrostyLoadReferenceImages": FrostyLoadReferenceImages,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "FrostyViggleTurboLora": "Frosty · Viggle Turbo v0.2.1 LoRA (unmerged)",
    "FrostyViggleTurboSigmas": "Frosty · Viggle Turbo Sigmas",
    "FrostyLoadReferenceImages": "Frosty · Ordered reference images",
}
