"""ComfyUI execution events; render completion remains an HTTP history check."""
from __future__ import annotations

import json
import math
import urllib.parse
from collections.abc import Mapping


SAMPLERS = {"KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced"}
PHASES = {
    "UnetLoaderGGUF": "Loading image model",
    "UNETLoader": "Loading image model",
    "CheckpointLoaderSimple": "Loading image model",
    "CLIPLoader": "Loading text encoder",
    "DualCLIPLoader": "Loading text encoder",
    "VAELoader": "Loading VAE",
    "TextEncodeQwenImage21": "Encoding prompt and references",
    "TextEncodeQwenImageEdit": "Encoding prompt and references",
    "CLIPTextEncode": "Encoding prompt",
    "FrostyViggleTurboLora": "Preparing Turbo adapter",
    "FrostyViggleTurboSigmas": "Preparing sampling schedule",
    "VAEDecode": "Decoding image",
    "VAEDecodeTiled": "Decoding image",
    "PreviewImage": "Preparing image output",
    "SaveImage": "Preparing image output",
    "LoadImage": "Loading reference image",
    "FrostyLoadReferenceImages": "Loading reference images",
}


class ComfyProgress:
    """Bounded, nonblocking event reads with a best-effort connection."""

    def __init__(self, base_url: str, client_id: str, timeout: float = 2):
        parts = urllib.parse.urlsplit(base_url.rstrip("/") + "/ws")
        self.url = urllib.parse.urlunsplit(("wss" if parts.scheme == "https" else "ws",
                                           parts.netloc, parts.path,
                                           urllib.parse.urlencode({"clientId": client_id}), ""))
        self.timeout = timeout
        self.socket = None
        self.available = False

    def __enter__(self):
        try:
            from websockets.sync.client import connect
            self.socket = connect(self.url, proxy=None, open_timeout=self.timeout,
                                  close_timeout=1, max_size=16_000_000)
            self.available = True
        except Exception:
            # Lack of progress must never prevent an otherwise valid render.
            self.available = False
        return self

    def __exit__(self, *_):
        if self.socket is not None:
            try:
                self.socket.close()
            except Exception:
                pass
        self.socket = None
        self.available = False

    def events(self):
        if not self.available:
            return []
        events = []
        for _ in range(64):
            try:
                raw = self.socket.recv(timeout=0)
            except TimeoutError:
                break
            except Exception:
                self.available = False
                break
            if not isinstance(raw, str):  # Binary preview frames aren't progress.
                continue
            try:
                event = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(event, dict):
                events.append(event)
        return events


def event_updates(event, prompt_id, workflow):
    """Only the selected prompt's sampler can report denoising steps."""
    data = event.get("data")
    if not isinstance(data, Mapping) or data.get("prompt_id") != prompt_id:
        return []
    kind = event.get("type")
    if kind == "execution_start":
        return [{"stage": "Starting ComfyUI workflow", "progress_determinate": False}]
    if kind == "executing":
        node = workflow.get(str(data.get("node")))
        if not isinstance(node, Mapping):
            return []
        cls = node.get("class_type", "node")
        phase = "Preparing image sampling" if cls in SAMPLERS else PHASES.get(cls, "Running " + str(cls))
        return [{"stage": phase, "progress_determinate": False}]
    if kind == "progress":
        nodes = {str(data.get("node")): data}
    elif kind == "progress_state" and isinstance(data.get("nodes"), Mapping):
        nodes = {str(key): value for key, value in data["nodes"].items()
                 if isinstance(value, Mapping) and value.get("state") == "running"}
    else:
        return []
    updates = []
    for node_id, state in nodes.items():
        node = workflow.get(node_id)
        if not isinstance(node, Mapping) or node.get("class_type") not in SAMPLERS:
            continue
        value, maximum = state.get("value"), state.get("max")
        if type(value) not in (int, float) or type(maximum) not in (int, float):
            continue
        try:
            valid = (math.isfinite(value) and math.isfinite(maximum)
                     and maximum > 0 and 0 < value <= maximum
                     and int(value) == value and int(maximum) == maximum)
        except (OverflowError, ValueError):
            valid = False
        # A running node starts with a placeholder 0/1 before sampling begins.
        if not valid:
            continue
        updates.append({"stage": f"Sampling {int(value)} / {int(maximum)} steps",
                        "progress_determinate": True,
                        "sampling_step": int(value), "sampling_steps": int(maximum)})
    return updates
