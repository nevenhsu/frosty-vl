"""Ordered reference uploads through ComfyUI's standard image loader."""
import json

import nodes


class FrostyLoadReferenceImages:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"filenames": ("STRING", {"default": "[]"})}}

    RETURN_TYPES = ("IMAGE",) * 16
    RETURN_NAMES = tuple(f"image_{index}" for index in range(1, 17))
    FUNCTION = "load"
    CATEGORY = "image/frosty"

    @classmethod
    def VALIDATE_INPUTS(cls, filenames):
        try:
            names = json.loads(filenames)
        except (ValueError, TypeError):
            return "Reference filenames must be a JSON array"
        if not isinstance(names, list) or not 1 <= len(names) <= 16:
            return "Provide 1 to 16 reference filenames"
        for name in names:
            if not isinstance(name, str) or not name:
                return "Reference filenames must be non-empty strings"
            valid = nodes.LoadImage.VALIDATE_INPUTS(name)
            if valid is not True:
                return valid
        return True

    def load(self, filenames):
        valid = self.VALIDATE_INPUTS(filenames)
        if valid is not True:
            raise ValueError(valid)
        images = [nodes.LoadImage().load_image(name)[0] for name in json.loads(filenames)]
        return tuple(images + [None] * (16 - len(images)))
