"""Image request and file contracts, independent of the optional MCP SDK."""
import io

import pytest
from PIL import Image

from frosty_mcp.client import StudioClient
from frosty_mcp.models import ImageSpec


def test_image_schema_passes_engine_options_without_policy_caps():
    spec = ImageSpec(prompt="", images=["image_fixture"] * 17, width=4097,
                     height=4096, num_inference_steps=301, n=9,
                     true_cfg_scale=20, reference_resolution=768, dwm_scale=3)
    assert len(spec.images) == 17 and spec.n == 9
    assert spec.num_inference_steps == 301 and spec.width == 4097


@pytest.mark.parametrize("options", [{"width":0},{"n":0},{"num_inference_steps":0},
                                    {"true_cfg_scale":float("inf")},{"dwm_scale":float("nan")}])
def test_image_schema_keeps_structural_checks(options):
    with pytest.raises(ValueError):
        ImageSpec(prompt="", **options)


def test_valid_large_image_does_not_have_app_pixel_cap():
    data = io.BytesIO()
    Image.new("RGB", (5000, 4000), "white").save(data, format="PNG")
    assert StudioClient.encode_image(data.getvalue()).startswith("data:image/png;base64,")


def test_image_decode_still_refuses_invalid_bytes():
    with pytest.raises(ValueError):
        StudioClient.encode_image(b"not an image")


def test_gallery_binary_reuse_accepts_more_than_old_twelve_megabytes(monkeypatch):
    class Response:
        def __enter__(self):return self
        def __exit__(self, *_):return False
        def read(self, size):
            assert size == 36_000_001
            return b"x" * 12_000_001
    class Opener:
        def open(self, *args, **kwargs):return Response()
    monkeypatch.setattr("frosty_mcp.client.urllib.request.build_opener", lambda *args: Opener())
    assert len(StudioClient()._read("/api/images/files/fixture.png", binary=True)) == 12_000_001
