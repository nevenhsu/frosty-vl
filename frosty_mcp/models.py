"""Agent-facing schemas. The render engine validates its own final payload."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

Workspace = Literal['image', 'video']


class ImageSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt: str
    mode: Literal['auto', 'generate', 'edit', 'transparent', 'extract', 'masked', 'annotate'] = 'auto'
    images: list[str] = Field(default_factory=list,
        description='Ordered image gallery IDs, PNG/JPEG/WebP data URLs, or files inside configured input directories.')
    mask: str | None = Field(default=None, description='Mask image reference; white edits, black preserves.')
    preserve_unmasked: bool | None = None
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    num_inference_steps: int | None = Field(default=None, ge=1)
    seed: int | None = Field(default=None, ge=0, le=2**63-1)
    negative_prompt: str | None = None
    true_cfg_scale: FiniteFloat | None = None
    use_kv_cache: bool | None = None
    reference_resolution: int | None = Field(default=None, ge=1)
    n: int | None = Field(default=None, ge=1)
    enhance_prompt: bool | None = None
    auto_aspect_ratio: bool | None = None
    dwm_scale: FiniteFloat | None = Field(default=None,
        description='Omit to keep the image engine default; zero requests a clean baseline.')


class Scene(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt: str = Field(min_length=1, max_length=12000)
    duration: float = Field(default=5, ge=1, le=15)


class VideoSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['clip', 'scenes'] = 'clip'
    prompt: str | dict | None = Field(default=None, description='Clip direction; structured objects require prompt_format=json.')
    prompt_format: Literal['text', 'json'] = 'text'
    engine_id: str | None = None
    image: str | None = Field(default=None, description='One image gallery ID, data URL or allowed local file.')
    identity_lock: bool = False
    seed: int | None = Field(default=None, ge=0, le=2**31-1)
    duration_seconds: float | None = Field(default=None, ge=1, le=15)
    resolution: str | None = None
    aspect_ratio: str | None = None
    num_inference_steps: int | None = Field(default=None, ge=1, le=100)
    guidance_scale: float | None = Field(default=None, ge=0, le=30)
    negative_prompt: str | None = Field(default=None, max_length=4000)
    story: str | None = Field(default=None, max_length=12000)
    scenes: list[Scene] | None = Field(default=None, min_length=2, max_length=8)
    continuity: bool = True
    request_id: str | None = Field(default=None, pattern=r'^[a-zA-Z0-9_.:-]{1,100}$',
        description='Reuse with identical settings to avoid duplicate video submissions in this Studio session.')
