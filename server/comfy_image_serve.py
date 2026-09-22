"""Frosty Studio image adapter for an existing ComfyUI server.

The adapter deliberately treats a ComfyUI API-format workflow as an opaque
template.  Every value that can be changed is described by a node/input
binding in the configuration; the adapter never searches a workflow for a
"likely" node.  This makes changes to a user's Comfy graph explicit and keeps
the adapter useful with custom nodes.

ComfyUI is an optional image backend.  Importing this module without
``FVL_COMFY_IMAGE_CONFIG`` is therefore safe; the health endpoint reports the
missing configuration and image submission returns a clear 503 response.
"""
from __future__ import annotations

import base64
import copy
import io
import json
import os
import queue
import secrets
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .image_library import ImageLibrary, TYPES


MAX_BODY = 36_000_000
MAX_REFERENCES = 10
MAX_REFERENCE_BYTES = 12_000_000
CAPABILITIES = ["text_to_image", "image_edit", "multi_reference"]
_MISSING = object()


class ComfyConfigError(ValueError):
    """Raised when the adapter configuration cannot be used safely."""


class ComfyError(RuntimeError):
    """An HTTP or protocol failure returned by ComfyUI."""


class JobCancelled(RuntimeError):
    """Raised internally when a cancellation wins before output publication."""


def _first(mapping: Mapping[str, Any], *names: str, default: Any = _MISSING) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    if default is not _MISSING:
        return default
    raise ComfyConfigError("Missing configuration value: " + "/".join(names))


def _as_path(value: Any, base: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ComfyConfigError(f"{label} must be a non-empty path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _binding_parts(value: Any, label: str, allow_node_only: bool = False) -> tuple[str, str | None]:
    """Normalize one explicit ``{node_id, input}`` binding.

    A two-item list is accepted as a compact form for hand-written config.
    Strings are accepted only for output-node declarations, never for an
    input binding, so a missing input can never silently turn into a guess.
    """
    if isinstance(value, (list, tuple)) and len(value) == 2:
        node, input_name = value
    elif isinstance(value, Mapping):
        node = value.get("node_id", value.get("node"))
        input_name = value.get("input", value.get("input_name"))
    elif allow_node_only and isinstance(value, str):
        node, input_name = value, None
    else:
        raise ComfyConfigError(f"{label} must name an explicit node and input")
    if not isinstance(node, (str, int)) or not str(node):
        raise ComfyConfigError(f"{label} has an invalid node id")
    if input_name is None and allow_node_only:
        return str(node), None
    if not isinstance(input_name, str) or not input_name:
        raise ComfyConfigError(f"{label} must name an explicit input")
    return str(node), input_name


def _workflow_nodes(workflow: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    if not isinstance(workflow, Mapping) or not workflow:
        raise ComfyConfigError(f"{label} must be a non-empty ComfyUI API workflow")
    # API-format workflows have node IDs at the top level.  Reject a UI-format
    # export instead of trying to translate it or guessing where inputs live.
    for node_id, node in workflow.items():
        if not isinstance(node_id, str) or not isinstance(node, Mapping):
            raise ComfyConfigError(f"{label} is not a ComfyUI API-format workflow")
        if "inputs" not in node or not isinstance(node["inputs"], Mapping):
            raise ComfyConfigError(f"{label} node {node_id!r} has no API inputs")
    return workflow


def _normalise_bindings(raw: Any, workflow: Mapping[str, Any], label: str) -> dict[str, list[tuple[str, str]]]:
    if not isinstance(raw, Mapping) or not raw:
        raise ComfyConfigError(f"{label} bindings must be a non-empty object")
    aliases = {"num_inference_steps": "steps", "guidance_scale": "guidance",
               "true_cfg_scale": "guidance", "ref": "references", "reference_images": "references"}
    known = {"prompt", "negative_prompt", "seed", "steps", "width", "height", "guidance",
             "reference", "references", "image", "images", "output", *aliases}
    result: dict[str, list[tuple[str, str]]] = {}
    for field, value in raw.items():
        field = aliases.get(field, field)
        if field not in known:
            raise ComfyConfigError(f"{label} has unsupported binding {field!r}")
        values = value if isinstance(value, list) and not (len(value) == 2 and isinstance(value[0], (str, int))) else [value]
        parsed: list[tuple[str, str]] = []
        for index, item in enumerate(values):
            node, input_name = _binding_parts(item, f"{label}.{field}[{index}]", allow_node_only=field == "output")
            node_spec = workflow.get(node)
            if node_spec is None:
                raise ComfyConfigError(f"{label}.{field} references unknown node {node!r}")
            if input_name is not None and input_name not in node_spec["inputs"]:
                raise ComfyConfigError(f"{label}.{field} references missing input {node}.{input_name}")
            parsed.append((node, input_name or ""))
        result[field] = parsed
    if "prompt" not in result:
        raise ComfyConfigError(f"{label} must bind prompt explicitly")
    return result


@dataclass(frozen=True)
class WorkflowSpec:
    path: Path
    template: dict[str, Any]
    bindings: dict[str, list[tuple[str, str]]]


@dataclass(frozen=True)
class ComfyConfig:
    comfyui_url: str
    output_dir: Path
    workflows: dict[str, WorkflowSpec]
    request_timeout: float = 30.0
    poll_interval: float = 0.25
    poll_timeout: float = 86_400.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], source: str | os.PathLike[str] | None = None) -> "ComfyConfig":
        if not isinstance(raw, Mapping):
            raise ComfyConfigError("ComfyUI config must be an object")
        base = Path(source).expanduser().resolve().parent if source else Path.cwd()
        url = _first(raw, "comfyui_url", "url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ComfyConfigError("comfyui_url must be an http(s) URL")
        output_dir = _as_path(_first(raw, "output_dir"), base, "output_dir")

        workflow_paths: dict[str, Any] = {}
        nested = raw.get("workflows")
        if not isinstance(nested, Mapping):
            nested = raw.get("workflow_paths")
        if isinstance(nested, Mapping):
            workflow_paths.update(nested)
        for mode in ("t2i", "edit"):
            value = _first(raw, f"{mode}_workflow", f"{mode}_workflow_path",
                           f"{mode}_api_workflow", f"{mode}_api_workflow_path", f"{mode}_api_path", default=None)
            if value is not None:
                workflow_paths[mode] = value
        bindings_root = raw.get("bindings")
        if not isinstance(bindings_root, Mapping):
            bindings_root = {}
        workflows: dict[str, WorkflowSpec] = {}
        for mode in ("t2i", "edit"):
            path_value = workflow_paths.get(mode)
            if isinstance(path_value, Mapping):
                workflow_path = _first(path_value, "path", "workflow", "file")
                inline_workflow = path_value.get("template")
            else:
                workflow_path, inline_workflow = path_value, None
            if workflow_path is None and inline_workflow is None:
                raise ComfyConfigError(f"Missing {mode} workflow path")
            if inline_workflow is not None:
                template = dict(_workflow_nodes(inline_workflow, f"{mode} workflow"))
                resolved = base / f"<inline-{mode}>"
            else:
                path = _as_path(workflow_path, base, f"{mode}_workflow")
                try:
                    template = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise ComfyConfigError(f"Cannot read {mode} workflow {path}: {exc}") from exc
                _workflow_nodes(template, f"{mode} workflow")
                resolved = path
            mode_entry = bindings_root.get(mode)
            if isinstance(mode_entry, Mapping) and "bindings" in mode_entry:
                mode_bindings = mode_entry["bindings"]
            else:
                mode_bindings = mode_entry
            if mode_bindings is None:
                mode_bindings = raw.get(f"{mode}_bindings", raw.get(f"{mode}_inputs"))
            if mode_bindings is None and isinstance(workflow_paths.get(mode), Mapping):
                mode_bindings = workflow_paths[mode].get("bindings")
            bindings = _normalise_bindings(mode_bindings, template, f"{mode} workflow")
            if mode == "edit" and not any(bindings.get(key) for key in ("references", "reference", "images", "image")):
                raise ComfyConfigError("edit workflow must bind at least one reference image explicitly")
            workflows[mode] = WorkflowSpec(resolved, copy.deepcopy(dict(template)), bindings)
        timeout = float(raw.get("request_timeout", 30.0))
        poll_interval = float(raw.get("poll_interval", 0.25))
        poll_timeout = float(raw.get("poll_timeout", 86_400.0))
        if timeout <= 0 or poll_interval <= 0 or poll_timeout <= 0:
            raise ComfyConfigError("ComfyUI timeouts must be positive")
        return cls(url.rstrip("/"), output_dir, workflows, timeout, poll_interval, poll_timeout)


def load_config(path: str | os.PathLike[str] | None = None) -> ComfyConfig | None:
    """Load the configured adapter, returning ``None`` when unset."""
    path = path or os.environ.get("FVL_COMFY_IMAGE_CONFIG")
    if not path:
        return None
    config_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ComfyConfigError(f"Cannot read FVL_COMFY_IMAGE_CONFIG {config_path}: {exc}") from exc
    return ComfyConfig.from_mapping(raw, config_path)


class ComfyClient:
    """Small stdlib-only client for the ComfyUI HTTP API."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: Any = _MISSING,
                 query: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None,
                 timeout: float | None = None) -> Any:
        url = self.base_url + "/" + path.lstrip("/")
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = None
        request_headers = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        if payload is not _MISSING:
            if isinstance(payload, bytes):
                data = payload
            else:
                data = json.dumps(payload).encode("utf-8")
                request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
        except (urllib.error.URLError, OSError) as exc:
            raise ComfyError(str(exc)) from exc
        if "json" in content_type or (raw[:1] in (b"{", b"[")):
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ComfyError("ComfyUI returned invalid JSON") from exc
        return raw

    def prompt(self, workflow: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self._request("POST", "/prompt", {"prompt": workflow})
        if not isinstance(result, Mapping):
            raise ComfyError("ComfyUI /prompt returned a non-object response")
        if result.get("error") or result.get("node_errors"):
            raise ComfyError(_error_text(result))
        if not result.get("prompt_id"):
            raise ComfyError("ComfyUI /prompt did not return prompt_id")
        return result

    def health(self) -> None:
        result = self._request("GET", "/system_stats", timeout=min(2.0, self.timeout))
        if not isinstance(result, Mapping):
            raise ComfyError("ComfyUI /system_stats returned a non-object response")

    def history(self, prompt_id: str) -> Mapping[str, Any]:
        result = self._request("GET", "/history/" + urllib.parse.quote(prompt_id, safe=""))
        return result if isinstance(result, Mapping) else {}

    def view(self, filename: str, subfolder: str = "", output_type: str = "output") -> bytes:
        result = self._request("GET", "/view", query={"filename": filename, "subfolder": subfolder, "type": output_type})
        if not isinstance(result, bytes):
            raise ComfyError("ComfyUI /view did not return image bytes")
        return result

    def upload(self, data: bytes, filename: str) -> Mapping[str, Any]:
        boundary = "----frosty-comfy-" + secrets.token_hex(12)
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\n"
            "Content-Type: image/png\r\n\r\n".encode("utf-8"),
            data,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
        result = self._request("POST", "/upload/image", b"".join(parts),
                               headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        if not isinstance(result, Mapping):
            raise ComfyError("ComfyUI upload returned a non-object response")
        if result.get("error"):
            raise ComfyError(_error_text(result))
        return result

    def queue(self) -> Mapping[str, Any]:
        result = self._request("GET", "/queue")
        return result if isinstance(result, Mapping) else {}

    def delete_prompt(self, prompt_id: str) -> Any:
        return self._request("POST", "/queue", {"delete": [prompt_id]})

    def interrupt(self) -> Any:
        return self._request("POST", "/interrupt", {})


def _error_text(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("error", "message", "exception_message", "node_errors"):
            if value.get(key):
                item = value[key]
                if isinstance(item, Mapping):
                    return json.dumps(item, ensure_ascii=False)
                return str(item)
    return str(value)


class ImageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    prompt: str = Field(min_length=1, max_length=12000)
    mode: str = "auto"
    images_b64: list[str] = Field(default_factory=list, max_length=MAX_REFERENCES)
    negative_prompt: str = Field(default="", max_length=4000)
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1)
    steps: int = Field(default=30, alias="num_inference_steps", ge=1, le=200)
    width: int = Field(default=1024, ge=64, le=4096)
    height: int = Field(default=1024, ge=64, le=4096)
    guidance: float = Field(default=1.0, alias="true_cfg_scale", ge=0.0, le=30.0)
    n: int = Field(default=1, ge=1, le=8)

    @model_validator(mode="after")
    def validate_request(self):
        self.prompt = self.prompt.strip()
        self.mode = self.mode.strip().lower()
        if self.mode not in {"auto", "generate", "edit"}:
            raise ValueError("ComfyUI supports only auto, generate, and edit modes")
        if self.mode == "auto":
            self.mode = "edit" if self.images_b64 else "generate"
        if self.mode == "generate" and self.images_b64:
            raise ValueError("generate mode does not accept reference images; use edit")
        if self.mode == "edit" and not self.images_b64:
            raise ValueError("edit mode requires at least one reference image")
        if self.width % 8 or self.height % 8:
            raise ValueError("Width and height must be multiples of 8")
        if sum(len(item) for item in self.images_b64) > MAX_REFERENCES * MAX_REFERENCE_BYTES * 2:
            raise ValueError("Combined reference images are too large")
        return self


def _decode_reference(value: str) -> tuple[bytes, str]:
    if not isinstance(value, str):
        raise ValueError("Reference image must be base64 text")
    if len(value) > MAX_REFERENCE_BYTES:
        raise ValueError("A reference image exceeds 12 MB")
    head, encoded = (value.split(",", 1) if "," in value else ("", value))
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid reference image encoding") from exc
    if not data:
        raise ValueError("Reference image is empty")
    # Validate bytes before handing them to a remote service, and normalize all
    # uploads to PNG so extension and payload agree.
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("Use PNG, JPEG, or WebP references")
            if image.width * image.height > 16_000_000:
                raise ValueError("Reference images must be at most 16 megapixels")
            image.load()
            normalized = io.BytesIO()
            image.convert("RGBA").save(normalized, format="PNG")
            data = normalized.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError("Invalid reference image: " + str(exc)) from exc
    return data, "reference.png"


def _queue_ids(value: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(value, str):
        ids.add(value)
    elif isinstance(value, Mapping):
        for key in ("prompt_id", "id"):
            if value.get(key):
                ids.add(str(value[key]))
        for item in value.values():
            ids.update(_queue_ids(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            ids.update(_queue_ids(item))
    return ids


class ComfyImageEngine:
    """Bounded local queue and lifecycle adapter around ComfyUI."""

    def __init__(self, config: ComfyConfig | None = None, client: Any | None = None,
                 output_dir: str | os.PathLike[str] | None = None, autostart: bool = False,
                 config_error: str | None = None):
        if isinstance(config, Mapping):
            config = ComfyConfig.from_mapping(config)
        self.config = config
        self.error = config_error
        if config is not None:
            self.output = Path(output_dir or config.output_dir).resolve()
            self.client = client or ComfyClient(config.comfyui_url, config.request_timeout)
        else:
            self.output = Path(output_dir or "outputs/images").resolve()
            self.client = client
        self.output.mkdir(parents=True, exist_ok=True)
        self.library = ImageLibrary(self.output)
        self.jobs: dict[str, dict[str, Any]] = {}
        self.pending: queue.Queue[str] = queue.Queue(maxsize=4)
        self.lock = threading.RLock()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()

    @property
    def ready(self) -> bool:
        return self.config is not None and self.client is not None and not self.error

    def start(self) -> None:
        if not self.ready or (self.worker and self.worker.is_alive()):
            return
        self.stop_event.clear()
        self.worker = threading.Thread(target=self._worker, name="comfy-image-worker", daemon=True)
        self.worker.start()

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                job_id = self.pending.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if self.get(job_id).get("cancel"):
                    self.update(job_id, status="cancelled", stage="Cancelled")
                else:
                    self._run(job_id)
            except Exception as exc:
                traceback.print_exc()
                if job_id in self.jobs:
                    self.update(job_id, status="error", stage="Error", error=f"{type(exc).__name__}: {exc}")
            finally:
                self.pending.task_done()

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self.lock:
            if job_id not in self.jobs:
                return {}
            self.jobs[job_id].update(changes)
            self.jobs[job_id]["updated_at"] = time.time()
            return copy.deepcopy(self.jobs[job_id])

    def get(self, job_id: str, private: bool = False) -> dict[str, Any]:
        with self.lock:
            if not job_id or not job_id.startswith("img_"):
                raise HTTPException(404, "Unknown image job")
            if job_id not in self.jobs:
                raise HTTPException(404, "Unknown image job")
            result = copy.deepcopy(self.jobs[job_id])
            if not private:
                result.pop("_spec", None)
            return result

    def submit(self, spec: ImageRequest) -> dict[str, Any]:
        if not isinstance(spec, ImageRequest):
            spec = ImageRequest.model_validate(spec)
        if not self.ready:
            raise HTTPException(503, self.error or "ComfyUI image adapter is not configured")
        with self.lock:
            if self.pending.full():
                raise HTTPException(429, "The image queue is full; wait for a render to finish")
            while len(self.jobs) >= 100:
                old = next((key for key, value in self.jobs.items()
                            if value["status"] in {"done", "error", "cancelled"}), None)
                if old is None:
                    break
                self.jobs.pop(old)
            job_id = "img_" + secrets.token_hex(12)
            seed = spec.seed if spec.seed is not None else secrets.randbelow(2**31)
            self.jobs[job_id] = {
                "id": job_id, "kind": "image", "mode": spec.mode,
                "status": "queued", "stage": "Queued", "seed": seed,
                "created_at": time.time(), "updated_at": time.time(), "outputs": [],
                "cancel": False, "completed_steps": 0, "total_steps": spec.steps * spec.n,
                "prompt_ids": [], "published": [],
                "_spec": spec,
            }
            try:
                self.pending.put_nowait(job_id)
            except queue.Full:
                self.jobs.pop(job_id, None)
                raise HTTPException(429, "The image queue is full; wait for a render to finish")
        return self.get(job_id)

    def cancel(self, job_id: str) -> dict[str, Any]:
        current = self.get(job_id)
        if current["status"] in {"done", "error", "cancelled"}:
            return current
        self.update(job_id, cancel=True, stage="Cancelling")
        prompt_ids = current.get("prompt_ids", [])
        if not prompt_ids:
            # The prompt has not reached ComfyUI.  The worker consumes and
            # discards it; no remote cancellation call is needed.
            return self.update(job_id, status="cancelled", stage="Cancelled")
        for prompt_id in prompt_ids:
            self._cancel_remote(job_id, prompt_id)
        return self.get(job_id)

    def _cancel_remote(self, job_id: str, prompt_id: str) -> None:
        try:
            state = self.client.queue()
        except Exception as exc:
            self.update(job_id, cancel_error=f"Cannot inspect ComfyUI queue: {exc}")
            return
        running = _queue_ids(state.get("queue_running", [])) if isinstance(state, Mapping) else set()
        pending = _queue_ids(state.get("queue_pending", [])) if isinstance(state, Mapping) else set()
        try:
            if prompt_id in pending:
                self.client.delete_prompt(prompt_id)
                self.update(job_id, status="cancelled", stage="Cancelled")
            elif prompt_id in running:
                # ComfyUI's /interrupt is global and can race with unrelated
                # clients. Let this prompt finish, then discard its result.
                self.update(job_id, status="cancelling", stage="Cancelling after the current ComfyUI render")
            else:
                self.update(job_id, status="cancelling", stage="Waiting for ComfyUI cancellation")
        except Exception as exc:
            self.update(job_id, cancel_error=str(exc))

    def _binding_value(self, bindings: Mapping[str, list[tuple[str, str]]], field: str) -> list[tuple[str, str]]:
        values = bindings.get(field)
        if values:
            return values
        aliases = {"references": ("reference", "images", "image"), "reference": ("references", "images", "image"),
                   "images": ("references", "reference", "image"), "image": ("references", "reference", "images")}
        for alias in aliases.get(field, ()):
            if bindings.get(alias):
                return bindings[alias]
        return []

    def _inject(self, workflow: dict[str, Any], spec: ImageRequest, bindings: Mapping[str, list[tuple[str, str]]],
                seed: int, references: list[str]) -> None:
        values: dict[str, Any] = {"prompt": spec.prompt, "negative_prompt": spec.negative_prompt,
                                  "seed": seed, "steps": spec.steps, "width": spec.width,
                                  "height": spec.height, "guidance": spec.guidance}
        for field, value in values.items():
            targets = self._binding_value(bindings, field)
            if not targets:
                # Reference-derived edit workflows can determine their latent
                # dimensions from the uploaded image.  The Studio request
                # still carries its usual width/height fields, but those
                # fields must not make such a workflow invalid.  Text-to-
                # image workflows remain required to bind both dimensions.
                if field in {"width", "height"} and spec.mode == "edit" and spec.images_b64:
                    continue
                # Optional fields are not guessed or silently redirected.  A
                # non-default request makes the missing explicit binding an
                # actionable configuration error.
                default = {"negative_prompt": "", "guidance": 1.0}.get(field)
                if field == "prompt" or value != default and field in {"negative_prompt", "seed", "steps", "width", "height", "guidance"}:
                    raise ComfyConfigError(f"No explicit binding configured for {field}")
                continue
            for node, input_name in targets:
                workflow[node]["inputs"][input_name] = value
        if references:
            targets = self._binding_value(bindings, "references")
            if not targets:
                raise ComfyConfigError("Edit workflow has no explicit reference binding")
            # A list of bindings maps one-to-one in reference order.  A single
            # binding receives the ordered list, which is supported by custom
            # nodes and preserves all source ordering information.
            if len(targets) == 1:
                node, input_name = targets[0]
                workflow[node]["inputs"][input_name] = references if len(references) > 1 else references[0]
            elif len(targets) != len(references):
                raise ComfyConfigError("Reference binding count does not match uploaded references")
            else:
                for (node, input_name), filename in zip(targets, references):
                    workflow[node]["inputs"][input_name] = filename

    def build_workflow(self, spec: ImageRequest, seed: int, references: list[str] | None = None) -> dict[str, Any]:
        mode = "t2i" if spec.mode == "generate" else spec.mode
        if not self.config or mode not in self.config.workflows:
            raise ComfyConfigError(f"No configured workflow for mode {mode}")
        workflow_spec = self.config.workflows[mode]
        workflow = copy.deepcopy(workflow_spec.template)
        self._inject(workflow, spec, workflow_spec.bindings, seed, references or [])
        return workflow

    def _upload_references(self, job_id: str, spec: ImageRequest) -> list[str]:
        uploaded: list[str] = []
        for index, encoded in enumerate(spec.images_b64):
            data, _ = _decode_reference(encoded)
            response = self.client.upload(data, f"frosty_{job_id}_{index:02d}.png")
            filename = response.get("name", response.get("filename")) if isinstance(response, Mapping) else None
            if not filename:
                raise ComfyError("ComfyUI upload did not return a filename")
            # Comfy LoadImage wants the filename only; subfolder is represented
            # separately by some workflows but is not lossy for API inputs.
            subfolder = response.get("subfolder", "") if isinstance(response, Mapping) else ""
            uploaded.append(f"{subfolder}/{filename}" if subfolder else str(filename))
        return uploaded

    def _run(self, job_id: str) -> None:
        job = self.get(job_id, private=True)
        spec = _job_spec(job)
        started = time.monotonic()
        self.update(job_id, status="running", stage="Uploading references")
        references = self._upload_references(job_id, spec) if spec.images_b64 else []
        for index in range(spec.n):
            current = self.get(job_id)
            if current.get("cancel"):
                self.update(job_id, status="cancelled", stage="Cancelled")
                return
            seed = (job["seed"] + index) % (2**63)
            workflow = self.build_workflow(spec, seed, references)
            self.update(job_id, stage=f"Queueing image {index + 1} of {spec.n}")
            response = self.client.prompt(workflow)
            prompt_id = str(response["prompt_id"])
            self.update(job_id, prompt_ids=self.get(job_id).get("prompt_ids", []) + [prompt_id], stage="Waiting for ComfyUI")
            result = self._wait_prompt(job_id, prompt_id, spec, seed, index, started)
            if result == "cancelled":
                return
            if result == "error":
                return
        if self.get(job_id).get("cancel"):
            self.update(job_id, status="cancelled", stage="Cancelled")
            return
        self.update(job_id, status="done", stage="Complete", seconds=round(time.monotonic() - started, 2),
                    completed_steps=spec.steps * spec.n)

    def _wait_prompt(self, job_id: str, prompt_id: str, spec: ImageRequest, seed: int,
                     variation: int, started: float) -> str:
        deadline = time.monotonic() + (self.config.poll_timeout if self.config else 86_400)
        while time.monotonic() < deadline:
            job = self.get(job_id)
            if job.get("cancel") and job.get("status") == "cancelled":
                return "cancelled"
            if job.get("cancel") and job.get("status") not in {"cancelled", "cancelling"}:
                self._cancel_remote(job_id, prompt_id)
            try:
                history = self.client.history(prompt_id)
            except Exception as exc:
                if self.get(job_id).get("cancel"):
                    self.update(job_id, status="cancelled", stage="Cancelled")
                    return "cancelled"
                self.update(job_id, status="error", stage="ComfyUI error", error=str(exc))
                return "error"
            job = self.get(job_id)
            if job.get("cancel"):
                self.update(job_id, status="cancelled", stage="Cancelled")
                return "cancelled"
            entry = history.get(prompt_id, history) if isinstance(history, Mapping) else {}
            status = entry.get("status", {}) if isinstance(entry, Mapping) else {}
            status_name = str(status.get("status_str", "")).lower() if isinstance(status, Mapping) else ""
            completed = bool(status.get("completed")) if isinstance(status, Mapping) else False
            if status_name in {"error", "failed"} or (isinstance(entry, Mapping) and entry.get("error")):
                if job.get("cancel"):
                    self.update(job_id, status="cancelled", stage="Cancelled")
                    return "cancelled"
                self.update(job_id, status="error", stage="ComfyUI error", error=_error_text(entry))
                return "error"
            outputs = entry.get("outputs", {}) if isinstance(entry, Mapping) else {}
            if completed or status_name in {"success", "done"}:
                if job.get("cancel"):
                    self.update(job_id, status="cancelled", stage="Cancelled")
                    return "cancelled"
                try:
                    self._publish_outputs(job_id, spec, seed, prompt_id, outputs, variation)
                except JobCancelled:
                    self.update(job_id, status="cancelled", stage="Cancelled")
                    return "cancelled"
                except Exception as exc:
                    self.update(job_id, status="error", stage="Publish error", error=f"{type(exc).__name__}: {exc}")
                    return "error"
                return "done"
            if outputs and (status_name in {"success", "done"} or not status):
                # Older Comfy versions omit status.completed once outputs are
                # available; success plus outputs is still terminal.
                try:
                    self._publish_outputs(job_id, spec, seed, prompt_id, outputs, variation)
                except JobCancelled:
                    self.update(job_id, status="cancelled", stage="Cancelled")
                    return "cancelled"
                return "done"
            self.update(job_id, completed_steps=min(spec.steps * (variation + 1),
                                                    self.get(job_id).get("completed_steps", 0) + 1))
            time.sleep(self.config.poll_interval if self.config else 0.25)
        if self.get(job_id).get("cancel"):
            self.update(job_id, status="cancelled", stage="Cancelled")
            return "cancelled"
        self.update(job_id, status="error", stage="ComfyUI timeout", error="Timed out waiting for ComfyUI history")
        return "error"

    def _publish_outputs(self, job_id: str, spec: ImageRequest, seed: int, prompt_id: str,
                         outputs: Mapping[str, Any], variation: int) -> None:
        if not isinstance(outputs, Mapping):
            raise ComfyError("ComfyUI history returned invalid outputs")
        descriptors: list[tuple[str, Mapping[str, Any]]] = []
        output_nodes = set()
        workflow_mode = "t2i" if spec.mode == "generate" else spec.mode
        if self.config and workflow_mode in self.config.workflows:
            output_nodes = {node for node, _ in self.config.workflows[workflow_mode].bindings.get("output", [])}
        for node_id, node_output in outputs.items():
            if output_nodes and str(node_id) not in output_nodes:
                continue
            if not isinstance(node_output, Mapping):
                continue
            for descriptor in node_output.get("images", []):
                if isinstance(descriptor, Mapping) and descriptor.get("filename"):
                    key = (prompt_id, str(node_id), str(descriptor.get("filename")),
                           str(descriptor.get("subfolder", "")), str(descriptor.get("type", "output")))
                    descriptors.append(("|".join(key), descriptor))
        if not descriptors:
            raise ComfyError("ComfyUI completed without an image output")
        existing = self.get(job_id)
        published = set(existing.get("published", []))
        for key, descriptor in descriptors:
            if key in published:
                continue
            if self.get(job_id).get("cancel"):
                raise JobCancelled()
            raw = self.client.view(str(descriptor["filename"]), str(descriptor.get("subfolder", "")), str(descriptor.get("type", "output")))
            with Image.open(io.BytesIO(raw)) as source:
                image = source.convert("RGBA").copy()
            name = f"image_{time.strftime('%Y%m%d_%H%M%S')}_{seed}_{secrets.token_hex(4)}.png"
            metadata = {
                "prompt": spec.prompt, "effective_prompt": spec.prompt, "negative_prompt": spec.negative_prompt,
                "seed": seed, "mode": spec.mode,
                "width": image.width, "height": image.height, "num_inference_steps": spec.steps,
                "guidance": spec.guidance, "reference_count": len(spec.images_b64),
                "engine_id": "comfyui", "engine_label": "ComfyUI", "comfy_prompt_id": prompt_id,
                "variation": variation, "created_at": time.time(),
            }
            with self.lock:
                if self.jobs[job_id].get("cancel"):
                    raise JobCancelled()
                saved = self.library.publish(image, name, metadata)
                output = {"id": saved["id"], "name": name, "file_url": "/files/" + name,
                          "seed": seed, "width": image.width, "height": image.height}
                published.add(key)
                self.update(job_id, outputs=self.get(job_id).get("outputs", []) + [output],
                            published=sorted(published), seconds=round(time.monotonic() - self.get(job_id)["created_at"], 2))


def _job_spec(job: Mapping[str, Any]) -> ImageRequest:
    # Specs stay private on the engine to keep API responses compact.
    return job["_spec"]


def _load_global() -> tuple[ComfyConfig | None, str | None]:
    try:
        return load_config(), None
    except Exception as exc:
        # A malformed optional config should make the service unhealthy, not
        # prevent the Studio module from importing and exposing diagnostics.
        return None, str(exc)


CONFIG, CONFIG_ERROR = _load_global()
engine = ComfyImageEngine(CONFIG, config_error=CONFIG_ERROR)


app = FastAPI(title="Frosty Studio — ComfyUI Image")


@app.middleware("http")
async def body_limit(request: Request, call_next):
    if request.method == "POST":
        chunks, total = [], 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_BODY:
                return JSONResponse({"detail": "Request exceeds 36 MB"}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
    return await call_next(request)


@app.on_event("startup")
def startup():
    engine.start()


@app.get("/health")
def health():
    edit_refs = []
    if engine.config and "edit" in engine.config.workflows:
        edit_bindings = engine.config.workflows["edit"].bindings
        for key in ("references", "reference", "images", "image"):
            if edit_bindings.get(key):
                edit_refs = edit_bindings[key]
                break
    max_references = len(edit_refs) if edit_refs else 0
    capabilities = ["text_to_image", "image_edit"]
    if max_references > 1:
        capabilities.append("multi_reference")
    controls = {"max_references": max_references,
                "resolutions": [384, 512, 1024, 2048],
                "resolution_default": 384,
                "edit_reference_resolution": 384,
                "steps": {"min": 1, "max": 200, "default": 20},
                "guidance": {"min": 0, "max": 30, "default": 1, "step": 0.5}}
    ready = engine.ready
    upstream_error = engine.error
    if ready and isinstance(engine.client, ComfyClient):
        try:
            engine.client.health()
        except Exception as exc:
            ready = False
            upstream_error = "ComfyUI unavailable: " + str(exc)
    return {"status": "error" if upstream_error else "ok" if ready else "loading",
            "ready": ready, "loading": bool(engine.worker and engine.worker.is_alive()),
            "error": upstream_error, "model": "ComfyUI", "capabilities": capabilities,
            "controls": controls,
            "active_job": next((key for key, value in engine.jobs.items() if value["status"] in {"running", "cancelling"}), None),
            "queued": engine.pending.qsize()}


@app.post("/jobs", status_code=202)
def submit(spec: ImageRequest):
    try:
        return engine.submit(spec)
    except (ComfyConfigError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/jobs/{job_id}")
def job(job_id: str):
    return engine.get(job_id)


@app.post("/jobs/{job_id}/cancel")
def cancel(job_id: str):
    return engine.cancel(job_id)


class LibrarySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1, max_length=100)


@app.get("/gallery")
def gallery():
    return engine.library.gallery()


@app.get("/gallery/trash")
def gallery_trash():
    return engine.library.trash_items()


def library_action(selection: LibrarySelection, action):
    results = []
    for identifier in dict.fromkeys(selection.ids):
        try:
            entry = action(identifier)
            results.append({"id": identifier, "ok": True, "trash_id": entry["id"],
                            "asset_id": entry["asset_id"], "name": entry["name"], "state": entry["state"]})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            results.append({"id": identifier, "ok": False, "error": str(exc)})
    return {"ok": all(item["ok"] for item in results), "results": results}


@app.post("/gallery/trash")
def trash_images(selection: LibrarySelection):
    return library_action(selection, engine.library.move_to_trash)


@app.post("/gallery/restore")
def restore_images(selection: LibrarySelection):
    return library_action(selection, engine.library.restore)


@app.get("/files/{name}")
def output_file(name: str):
    try:
        candidate = engine.library.file(name)
    except (OSError, ValueError):
        raise HTTPException(404, "Image not found")
    return FileResponse(candidate, media_type=TYPES[candidate.suffix.lower()])
