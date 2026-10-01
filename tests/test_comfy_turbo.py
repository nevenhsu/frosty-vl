"""Sampling constraints must agree between Studio health, requests and graphs."""
import json
import base64
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from server import comfy_image_serve as comfy


ROOT = Path(__file__).resolve().parents[1]


def turbo_engine(tmp_path):
    config = comfy.load_config(ROOT / "config/comfyui-image-turbo.json")
    return comfy.ComfyImageEngine(config, client=object(), output_dir=tmp_path)


def test_default_steps_are_applied_before_queueing_and_progress(tmp_path):
    engine = turbo_engine(tmp_path)
    job = engine.submit(comfy.ImageRequest(prompt="A cup", width=384, height=384))
    assert job["total_steps"] == 6
    request = engine.get(job["id"], private=True)["_spec"]
    assert request.steps == 6
    graph = engine.build_workflow(request, 42)
    assert graph["481"]["inputs"]["steps"] == 6
    assert graph["483"]["inputs"]["noise_seed"] == 42


@pytest.mark.parametrize("options", [
    {"true_cfg_scale": 2}, {"negative_prompt": "blurred"},
])
def test_invalid_turbo_options_are_rejected_before_enqueue(tmp_path, monkeypatch, options):
    engine = turbo_engine(tmp_path)
    monkeypatch.setattr(comfy, "engine", engine)
    response = TestClient(comfy.app).post("/jobs", json={"prompt": "A cup", **options})
    assert response.status_code == 422
    assert not engine.jobs
    assert engine.pending.empty()


def test_eight_steps_and_health_controls_agree(tmp_path, monkeypatch):
    engine = turbo_engine(tmp_path)
    monkeypatch.setattr(comfy, "engine", engine)
    client = TestClient(comfy.app)
    health = client.get("/health").json()
    assert health["profile"] == "turbo"
    assert health["controls"]["resolution_default"] == 512
    assert health["controls"]["edit_reference_resolution"] == 512
    assert health["controls"]["steps"] == {"min": 1, "default": 6, "step": 1}
    assert health["controls"]["guidance"]["min"] == health["controls"]["guidance"]["max"] == 1
    assert health["controls"]["negative_prompt"] is False
    for mode in ("t2i", "edit", "masked"):
        template = engine.config.workflows[mode].template
        assert template["480"]["inputs"]["strength"] == 1
        assert template["482"]["class_type"] == "BasicGuider"
        assert template["484"]["inputs"]["sampler_name"] == "euler"
    spec = comfy.ImageRequest(prompt="A cup", num_inference_steps=8, width=384, height=384)
    assert engine.submit(spec)["total_steps"] == 8
    assert engine.build_workflow(spec, 1)["481"]["inputs"]["steps"] == 8
    assert engine.config.workflows["t2i"].template["481"]["inputs"]["steps"] == 6


def test_base_requests_remain_unrestricted(tmp_path):
    config = comfy.load_config(ROOT / "config/comfyui-image.json")
    engine = comfy.ComfyImageEngine(config, client=object(), output_dir=tmp_path)
    for steps in (7, 20, 40):
        request = comfy.ImageRequest(prompt="A cup", steps=steps, guidance=2, negative_prompt="blurred")
        graph = engine.build_workflow(request, 1)
        assert graph["458"]["inputs"]["steps"] == steps
        assert graph["458"]["inputs"]["cfg"] == 2


@pytest.mark.parametrize("steps", [1, 4, 5, 7, 8, 20, 200, 301])
def test_turbo_accepts_requested_steps_without_rewriting(tmp_path, monkeypatch, steps):
    engine = turbo_engine(tmp_path)
    monkeypatch.setattr(comfy, "engine", engine)
    response = TestClient(comfy.app).post("/jobs", json={"prompt": "A cup", "steps": steps})
    assert response.status_code == 202
    job = response.json()
    assert job["total_steps"] == steps
    spec = engine.get(job["id"], private=True)["_spec"]
    assert engine.build_workflow(spec, 1)["481"]["inputs"]["steps"] == steps


def test_old_step_list_does_not_restrict_requests(tmp_path, monkeypatch):
    source = ROOT / "config/comfyui-image-turbo.json"
    raw = json.loads(source.read_text())
    raw["sampling"]["steps"] = [6, 8]
    config = comfy.ComfyConfig.from_mapping(raw, source)
    engine = comfy.ComfyImageEngine(config, client=object(), output_dir=tmp_path)
    monkeypatch.setattr(comfy, "engine", engine)
    client = TestClient(comfy.app)
    assert "values" not in client.get("/health").json()["controls"]["steps"]
    assert client.post("/jobs", json={"prompt": "A cup", "steps": 7}).status_code == 202


def test_generated_raw_nodes_preserve_upstream_examples_and_requested_count():
    import importlib.util
    source = ROOT / "comfyui/frosty_viggle_turbo_v021/schedules.py"
    spec = importlib.util.spec_from_file_location("turbo_schedules_test", source)
    schedules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schedules)
    assert schedules.raw_nodes(5) == [1, .875, .75, .5, .25]
    assert schedules.raw_nodes(6) == [1, .9375, .875, .75, .5, .25]
    assert schedules.raw_nodes(7) == [1, .9583, .9167, .875, .75, .5, .25]
    for steps in (1, 4, 5, 6, 7, 8, 20, 200, 301):
        values = schedules.raw_nodes(steps)
        assert len(values) == steps and values[0] == 1
        assert all(a > b > 0 for a, b in zip(values, values[1:]))
        if steps >= 5:
            assert values[-4:] == [.875, .75, .5, .25]


@pytest.mark.parametrize("count", [1, 3, 10, 16])
def test_references_are_bound_in_order_as_json(tmp_path, count):
    engine = turbo_engine(tmp_path)
    data = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(data, format="PNG")
    encoded = base64.b64encode(data.getvalue()).decode()
    spec = comfy.ImageRequest(prompt="Combine these images", mode="edit", images_b64=[encoded]*count)
    names = [f"reference_{i}.png" for i in range(count)]
    assert engine.submit(spec)["total_steps"] == 6
    graph = engine.build_workflow(spec, 42, names)
    assert json.loads(graph["460"]["inputs"]["filenames"]) == names
    for index in range(16):
        assert graph["474"]["inputs"][f"images.image_{index+1}"] == ["460", index]


@pytest.mark.parametrize("mode,has_reference,has_mask", [
    ("auto", False, False), ("auto", True, False),
    ("transparent", False, False), ("transparent", True, False),
    ("extract", True, False), ("masked", True, True), ("annotate", True, False),
])
def test_every_image_workspace_uses_turbo_sampling(tmp_path, mode, has_reference, has_mask):
    engine = turbo_engine(tmp_path)
    data = io.BytesIO()
    Image.new("RGBA", (16, 16), "white").save(data, format="PNG")
    encoded = base64.b64encode(data.getvalue()).decode()
    spec = comfy.ImageRequest(prompt="A cup", mode=mode, steps=7,
                              images_b64=[encoded] if has_reference else [],
                              mask_b64=encoded if has_mask else None)
    graph = engine.build_workflow(spec, 42, ["reference.png"] if has_reference else [],
                                  "mask.png" if has_mask else None)
    assert graph["480"]["class_type"] == "FrostyViggleTurboLora"
    assert graph["481"]["class_type"] == "FrostyViggleTurboSigmas"
    assert graph["481"]["inputs"]["steps"] == 7
    assert graph["458"]["class_type"] == "SamplerCustomAdvanced"
    if has_reference:
        assert json.loads(graph["460"]["inputs"]["filenames"]) == ["reference.png"]
    if has_mask:
        assert graph["475"]["inputs"]["image"] == "mask.png"


def test_reference_capacity_and_mask_slot_match_existing_api(tmp_path, monkeypatch):
    engine = turbo_engine(tmp_path)
    monkeypatch.setattr(comfy, "engine", engine)
    health = TestClient(comfy.app).get("/health").json()
    assert health["controls"]["max_references"] == 16
    assert health["controls"]["max_references_by_mode"]["masked"] == 15
    assert "multi_reference" in health["capabilities"]
    data = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(data, format="PNG")
    encoded = base64.b64encode(data.getvalue()).decode()
    spec = comfy.ImageRequest(prompt="Replace the background", mode="masked", images_b64=[encoded]*15, mask_b64=encoded)
    graph = engine.build_workflow(spec, 42, [f"reference_{i}.png" for i in range(15)], "mask.png")
    assert graph["474"]["inputs"]["images.image_16"] == ["475", 0]
    assert graph["475"]["inputs"]["image"] == "mask.png"
    with pytest.raises(ValueError, match="up to 15 references"):
        comfy.ImageRequest(prompt="Replace", mode="masked", images_b64=[encoded]*16, mask_b64=encoded)


def test_output_override_follows_selected_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("FVL_COMFY_IMAGE_OUTPUT_DIR", str(tmp_path / "custom-gallery"))
    config = comfy.load_config(ROOT / "config/comfyui-image-turbo.json")
    assert config.output_dir == tmp_path / "custom-gallery"


@pytest.mark.parametrize("override", [
    {"default_steps": 0}, {"guidance": float("nan")}, {"negative_prompt": "false"},
])
def test_invalid_sampling_config_fails_closed(override):
    source = ROOT / "config/comfyui-image-turbo.json"
    raw = json.loads(source.read_text())
    raw["sampling"].update(override)
    with pytest.raises(comfy.ComfyConfigError):
        comfy.ComfyConfig.from_mapping(raw, source)


def test_no_artificial_dimension_area_variation_or_prompt_caps(tmp_path):
    engine = turbo_engine(tmp_path)
    spec = comfy.ImageRequest(prompt="", width=8193, height=4097, steps=301, n=9)
    graph = engine.build_workflow(spec, 42)
    assert graph["456"]["inputs"]["width"] == 8193
    assert graph["456"]["inputs"]["height"] == 4097
    assert graph["481"]["inputs"]["steps"] == 301
    assert engine.submit(spec)["total_steps"] == 2709


@pytest.mark.parametrize("options", [{"steps":0},{"n":0},{"width":0},{"guidance":float("inf")}])
def test_invalid_numeric_shapes_still_fail(options):
    with pytest.raises(ValueError):
        comfy.ImageRequest(prompt="A cup", **options)


def test_shipped_engine_controls_match_adapter_health(tmp_path, monkeypatch):
    monkeypatch.setattr(comfy, "engine", turbo_engine(tmp_path))
    live = TestClient(comfy.app).get("/health").json()["controls"]
    shipped = json.loads((ROOT / "config/comfyui-image-turbo-engines.json").read_text())["engines"][0]["controls"]
    for key in ("max_references", "max_references_by_mode", "steps"):
        assert shipped[key] == live[key]
