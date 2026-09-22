import base64
import io
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from server import comfy_image_serve as comfy


def png_data(color=(40, 80, 120, 255), size=(16, 12)):
    stream = io.BytesIO()
    Image.new("RGBA", size, color).save(stream, format="PNG")
    return stream.getvalue()


def _png_bytes(image):
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def data_url(data=None):
    return "data:image/png;base64," + base64.b64encode(data or png_data()).decode()


def workflow(include_image=False):
    nodes = {
        "prompt": {"class_type": "CLIPTextEncode", "inputs": {"text": "template prompt"}},
        "negative": {"class_type": "CLIPTextEncode", "inputs": {"text": "template negative"}},
        "seed": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 30, "cfg": 1.0}},
        "size": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024}},
        "save": {"class_type": "PreviewImage", "inputs": {"images": ["size", 0]}},
    }
    if include_image:
        nodes["image"] = {"class_type": "LoadImage", "inputs": {"image": "template.png"}}
    return nodes


def config_mapping(tmp_path, include_image=True):
    t2i = tmp_path / "t2i.json"
    edit = tmp_path / "edit.json"
    t2i.write_text(json.dumps(workflow()), encoding="utf-8")
    edit.write_text(json.dumps(workflow(include_image)), encoding="utf-8")
    fields = {
        "prompt": {"node_id": "prompt", "input": "text"},
        "negative_prompt": {"node_id": "negative", "input": "text"},
        "seed": {"node_id": "seed", "input": "seed"},
        "steps": {"node_id": "seed", "input": "steps"},
        "width": {"node_id": "size", "input": "width"},
        "height": {"node_id": "size", "input": "height"},
        "guidance": {"node_id": "seed", "input": "cfg"},
        "output": {"node_id": "save"},
    }
    edit_fields = dict(fields, references={"node_id": "image", "input": "image"})
    return {
        "comfyui_url": "http://127.0.0.1:8188",
        "output_dir": str(tmp_path / "outputs"),
        "t2i_workflow": str(t2i),
        "edit_workflow": str(edit),
        "bindings": {"t2i": fields, "edit": edit_fields},
        "poll_interval": 0.001,
        "poll_timeout": 1,
    }


def advanced_config_mapping(tmp_path):
    mapping = config_mapping(tmp_path)
    masked = tmp_path / "masked.json"
    masked_nodes = workflow(include_image=True)
    masked_nodes["mask"] = {"class_type": "LoadImage", "inputs": {"image": "mask.png"}}
    masked.write_text(json.dumps(masked_nodes), encoding="utf-8")
    base_bindings = mapping.pop("bindings")
    mapping["workflows"] = {
        "t2i": {"path": mapping.pop("t2i_workflow"), "bindings": base_bindings["t2i"]},
        "edit": {"path": mapping.pop("edit_workflow"), "bindings": base_bindings["edit"]},
        "masked": {"path": str(masked), "bindings": {
            **{key: value for key, value in base_bindings["edit"].items() if key != "references"},
            "reference": {"node_id": "image", "input": "image"},
            "mask": {"node_id": "mask", "input": "image"},
        }},
    }
    mapping["supported_modes"] = ["transparent", "extract", "masked", "annotate"]
    return mapping


class FakeComfy:
    def __init__(self):
        self.uploads = []
        self.prompts = []
        self.views = []
        self.interrupts = 0
        self.deleted = []
        self.error = False

    def upload(self, data, filename):
        self.uploads.append((data, filename))
        return {"name": f"uploaded-{len(self.uploads)}.png", "subfolder": "input"}

    def prompt(self, workflow):
        self.prompts.append(workflow)
        prompt_id = f"prompt-{len(self.prompts)}"
        return {"prompt_id": prompt_id}

    def history(self, prompt_id):
        if self.error:
            return {prompt_id: {"status": {"status_str": "error"}, "error": "node failed"}}
        return {prompt_id: {"status": {"status_str": "success", "completed": True},
                            "outputs": {"save": {"images": [{"filename": f"{prompt_id}.png"}]}}}}

    def view(self, filename, subfolder="", output_type="output"):
        self.views.append((filename, subfolder, output_type))
        return png_data()

    def queue(self):
        return {"queue_running": [], "queue_pending": []}

    def delete_prompt(self, prompt_id):
        self.deleted.append(prompt_id)

    def interrupt(self):
        self.interrupts += 1


def run_job(engine, job_id):
    assert engine.pending.get_nowait() == job_id
    engine.pending.task_done()
    engine._run(job_id)


@pytest.fixture
def configured(tmp_path):
    config = comfy.ComfyConfig.from_mapping(config_mapping(tmp_path), tmp_path / "config.json")
    fake = FakeComfy()
    engine = comfy.ComfyImageEngine(config, fake, autostart=False)
    return engine, fake


def test_config_and_binding_validation(tmp_path):
    mapping = config_mapping(tmp_path)
    mapping["bindings"]["t2i"]["prompt"] = {"node_id": "missing", "input": "text"}
    with pytest.raises(comfy.ComfyConfigError, match="unknown node"):
        comfy.ComfyConfig.from_mapping(mapping)

    mapping = config_mapping(tmp_path)
    mapping["bindings"]["edit"].pop("prompt")
    with pytest.raises(comfy.ComfyConfigError, match="bind prompt"):
        comfy.ComfyConfig.from_mapping(mapping)

    mapping = config_mapping(tmp_path)
    mapping["bindings"]["edit"].pop("references")
    with pytest.raises(comfy.ComfyConfigError, match="reference image"):
        comfy.ComfyConfig.from_mapping(mapping)

    mapping = config_mapping(tmp_path)
    mapping["bindings"]["t2i"].pop("negative_prompt")
    engine = comfy.ComfyImageEngine(comfy.ComfyConfig.from_mapping(mapping), FakeComfy())
    with pytest.raises(comfy.ComfyConfigError, match="negative_prompt"):
        engine.build_workflow(comfy.ImageRequest(prompt="fox", negative_prompt="blurry"), 1)


def test_edit_reference_workflow_allows_unbound_dimensions(tmp_path):
    mapping = config_mapping(tmp_path)
    mapping["bindings"]["edit"].pop("width")
    mapping["bindings"]["edit"].pop("height")
    config = comfy.ComfyConfig.from_mapping(mapping, tmp_path / "config.json")
    engine = comfy.ComfyImageEngine(config, FakeComfy())

    spec = comfy.ImageRequest(prompt="edit", mode="edit", images_b64=[data_url()])
    built = engine.build_workflow(spec, 7, ["input/reference.png"])

    assert built["image"]["inputs"]["image"] == "input/reference.png"
    # The reference-derived workflow keeps its own dimensions; the request's
    # UI-supplied width/height do not need bindings in edit mode.
    assert built["size"]["inputs"]["width"] == 1024
    assert built["size"]["inputs"]["height"] == 1024


def test_t2i_workflow_still_requires_dimensions(tmp_path):
    mapping = config_mapping(tmp_path)
    mapping["bindings"]["t2i"].pop("width")
    mapping["bindings"]["t2i"].pop("height")
    config = comfy.ComfyConfig.from_mapping(mapping, tmp_path / "config.json")
    engine = comfy.ComfyImageEngine(config, FakeComfy())

    with pytest.raises(comfy.ComfyConfigError, match="width"):
        engine.build_workflow(comfy.ImageRequest(prompt="fox"), 1)


def test_advanced_mode_request_contract():
    transparent = comfy.ImageRequest(prompt="glass icon", mode="transparent")
    assert transparent.mode == "transparent"

    reference = data_url()
    assert comfy.ImageRequest(prompt="keep the bag", mode="extract", images_b64=[reference]).mode == "extract"
    assert comfy.ImageRequest(prompt="change the circled label", mode="annotate",
                               images_b64=[reference]).mode == "annotate"
    masked = comfy.ImageRequest(prompt="replace the cup", mode="masked", images_b64=[reference],
                                mask_b64=data_url(png_data((255, 255, 255, 255))))
    assert masked.preserve_unmasked is True

    with pytest.raises(ValueError, match="reference image"):
        comfy.ImageRequest(prompt="keep the bag", mode="extract")
    with pytest.raises(ValueError, match="mask"):
        comfy.ImageRequest(prompt="replace the cup", mode="masked", images_b64=[reference])
    with pytest.raises(ValueError, match="only in masked"):
        comfy.ImageRequest(prompt="new", mask_b64=data_url())


def test_config_accepts_opt_in_advanced_modes_and_mask_binding(tmp_path):
    config = comfy.ComfyConfig.from_mapping(advanced_config_mapping(tmp_path), tmp_path / "config.json")

    assert set(config.workflows) == {"t2i", "edit", "masked"}
    assert config.supported_modes == frozenset({"transparent", "extract", "masked", "annotate"})


def test_advanced_modes_route_and_inject_effective_prompts(tmp_path):
    config = comfy.ComfyConfig.from_mapping(advanced_config_mapping(tmp_path), tmp_path / "config.json")
    engine = comfy.ComfyImageEngine(config, FakeComfy())
    reference = data_url()

    transparent = engine.build_workflow(comfy.ImageRequest(prompt="glass icon", mode="transparent"), 1)
    assert "RGBA image with transparency" in transparent["prompt"]["inputs"]["text"]
    transparent_edit = engine.build_workflow(
        comfy.ImageRequest(prompt="keep the shape", mode="transparent", images_b64=[reference]), 2,
        ["input/original.png"])
    assert transparent_edit["image"]["inputs"]["image"] == "input/original.png"

    extracted = engine.build_workflow(
        comfy.ImageRequest(prompt="the red bag", mode="extract", images_b64=[reference]), 3,
        ["input/original.png"])
    assert extracted["prompt"]["inputs"]["text"].startswith("Extract the requested subject")

    annotated = engine.build_workflow(
        comfy.ImageRequest(prompt="change this label", mode="annotate", images_b64=[reference]), 4,
        ["input/annotated.png"])
    assert "Remove the annotation markings" in annotated["prompt"]["inputs"]["text"]

    masked = engine.build_workflow(
        comfy.ImageRequest(prompt="replace the cup", mode="masked", images_b64=[reference],
                           mask_b64=data_url(png_data((255, 255, 255, 255)))),
        5, ["input/original.png"], "input/mask.png")
    assert masked["image"]["inputs"]["image"] == "input/original.png"
    assert masked["mask"]["inputs"]["image"] == "input/mask.png"
    assert "region marked white" in masked["prompt"]["inputs"]["text"]


def test_masked_job_uploads_mask_separately_and_validates_pixels(tmp_path):
    config = comfy.ComfyConfig.from_mapping(advanced_config_mapping(tmp_path), tmp_path / "config.json")
    fake = FakeComfy()
    engine = comfy.ComfyImageEngine(config, fake)
    reference = data_url(png_data((10, 20, 30, 255)))
    mask = data_url(png_data((255, 255, 255, 255)))

    job = engine.submit(comfy.ImageRequest(prompt="replace", mode="masked", seed=9,
                                           images_b64=[reference], mask_b64=mask))
    run_job(engine, job["id"])

    assert [name for _, name in fake.uploads] == [
        f"frosty_{job['id']}_00.png", f"frosty_{job['id']}_mask.png"]
    assert fake.prompts[0]["image"]["inputs"]["image"] == "input/uploaded-1.png"
    assert fake.prompts[0]["mask"]["inputs"]["image"] == "input/uploaded-2.png"

    with pytest.raises(ValueError, match="mask is empty"):
        comfy.ImageRequest(prompt="replace", mode="masked", images_b64=[reference],
                           mask_b64=data_url(png_data((0, 0, 0, 255))))
    with pytest.raises(ValueError, match="same dimensions"):
        comfy.ImageRequest(prompt="replace", mode="masked", images_b64=[reference],
                           mask_b64=data_url(png_data((255, 255, 255, 255), (8, 8))))


def test_selected_workflow_filters_outputs_and_mask_preserves_unselected_pixels(tmp_path):
    config = comfy.ComfyConfig.from_mapping(advanced_config_mapping(tmp_path), tmp_path / "config.json")
    fake = FakeComfy()
    engine = comfy.ComfyImageEngine(config, fake)

    transparent_spec = comfy.ImageRequest(prompt="icon", mode="transparent", seed=1)
    transparent_job = engine.submit(transparent_spec)
    engine._publish_outputs(transparent_job["id"], transparent_spec, 1, "transparent-prompt", {
        "noise": {"images": [{"filename": "ignore.png"}]},
        "save": {"images": [{"filename": "keep.png"}]},
    }, 0)
    assert [name for name, _, _ in fake.views] == ["keep.png"]

    original = Image.new("RGBA", (4, 2), (0, 0, 255, 255))
    mask = Image.new("RGBA", (4, 2), (0, 0, 0, 255))
    for x in range(2):
        for y in range(2):
            mask.putpixel((x, y), (255, 255, 255, 255))
    # Match the bundled workflow's behavior: Comfy may render a smaller fixed
    # canvas than the uploaded source.  The final composite must retain the
    # source dimensions and untouched source pixels outside the mask.
    generated = png_data((255, 0, 0, 128), (2, 1))
    fake.view = lambda *_: generated
    spec = comfy.ImageRequest(prompt="replace", mode="masked", seed=2,
                              images_b64=[data_url(_png_bytes(original))],
                              mask_b64=data_url(_png_bytes(mask)), preserve_unmasked=True)
    job = engine.submit(spec)
    engine._publish_outputs(job["id"], spec, 2, "masked-prompt",
                            {"save": {"images": [{"filename": "masked.png"}]}}, 0)

    output_name = engine.get(job["id"])["outputs"][0]["name"]
    with Image.open(engine.output / output_name) as result:
        assert result.size == original.size
        assert result.convert("RGBA").getpixel((0, 0)) == (255, 0, 0, 128)
        assert result.convert("RGBA").getpixel((3, 0)) == (0, 0, 255, 255)
    item = next(entry for entry in engine.library.gallery()["items"] if entry["name"] == output_name)
    assert item["effective_prompt"].startswith("Edit the first image in the region marked white")
    assert item["preserve_unmasked"] is True


def test_workflow_is_deep_copied_and_typed_fields_injected(configured):
    engine, _ = configured
    spec = comfy.ImageRequest(prompt="  fox  ", seed=7, steps=12, width=512, height=768,
                              guidance=4.5, negative_prompt="blurry")
    first = engine.build_workflow(spec, 7)
    assert first["prompt"]["inputs"]["text"] == "fox"
    assert first["seed"]["inputs"] == {"seed": 7, "steps": 12, "cfg": 4.5}
    first["prompt"]["inputs"]["text"] = "mutated"
    second = engine.build_workflow(spec, 8)
    assert second["prompt"]["inputs"]["text"] == "fox"
    assert second["seed"]["inputs"]["seed"] == 8


def test_comfy_client_sends_multipart_bytes_without_json_encoding(monkeypatch):
    captured = {}

    class Response:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"name":"uploaded.png"}'

    def open_request(request, timeout):
        captured.update(request=request, timeout=timeout)
        return Response()

    monkeypatch.setattr(comfy.urllib.request, "urlopen", open_request)
    result = comfy.ComfyClient("http://127.0.0.1:8188").upload(png_data(), "reference.png")
    assert result["name"] == "uploaded.png"
    assert captured["request"].data.startswith(b"------frosty-comfy-")
    assert captured["request"].get_header("Content-type").startswith("multipart/form-data; boundary=")


def test_jpeg_reference_is_normalized_to_png():
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(stream, format="JPEG")
    normalized, name = comfy._decode_reference(data_url(stream.getvalue()))
    assert name == "reference.png"
    assert normalized.startswith(b"\x89PNG\r\n\x1a\n")


def test_t2i_edit_selection_upload_order_and_consecutive_seeds(configured):
    engine, fake = configured
    t2i = engine.submit(comfy.ImageRequest(prompt="new", seed=10, n=2))
    run_job(engine, t2i["id"])
    assert [job["seed"]["inputs"]["seed"] for job in fake.prompts] == [10, 11]
    assert fake.uploads == []

    fake.prompts.clear()
    edit = engine.submit(comfy.ImageRequest(prompt="combine", mode="edit", seed=20,
                                             images_b64=[data_url(png_data((1, 2, 3, 255))),
                                                         data_url(png_data((4, 5, 6, 255)))], n=1))
    run_job(engine, edit["id"])
    assert [name for _, name in fake.uploads] == [f"frosty_{edit['id']}_00.png", f"frosty_{edit['id']}_01.png"]
    assert fake.prompts[0]["image"]["inputs"]["image"] == ["input/uploaded-1.png", "input/uploaded-2.png"]


def test_done_error_and_cancel_mapping(configured):
    engine, fake = configured
    done = engine.submit(comfy.ImageRequest(prompt="done", seed=1))
    run_job(engine, done["id"])
    assert engine.get(done["id"])["status"] == "done"
    assert len(engine.get(done["id"])["outputs"]) == 1

    fake.error = True
    failed = engine.submit(comfy.ImageRequest(prompt="failed", seed=2))
    run_job(engine, failed["id"])
    assert engine.get(failed["id"])["status"] == "error"

    queued = engine.submit(comfy.ImageRequest(prompt="queued", seed=3))
    cancelled = engine.cancel(queued["id"])
    assert cancelled["status"] == "cancelled"
    assert fake.interrupts == 0


def test_running_cancel_never_uses_global_interrupt(configured):
    engine, fake = configured
    job = engine.submit(comfy.ImageRequest(prompt="running", seed=3))
    engine.update(job["id"], status="running", prompt_ids=["remote"])
    fake.queue = lambda: {"queue_running": [[1, "remote"]], "queue_pending": []}
    result = engine.cancel(job["id"])
    assert result["status"] == "cancelling"
    assert fake.interrupts == 0


def test_cancel_during_history_discards_completed_output(configured):
    engine, fake = configured
    job = engine.submit(comfy.ImageRequest(prompt="running", seed=3))

    def finish_after_cancel(prompt_id):
        engine.cancel(job["id"])
        return {prompt_id: {"status": {"status_str": "success", "completed": True},
                            "outputs": {"save": {"images": [{"filename": "discard.png"}]}}}}

    fake.history = finish_after_cancel
    run_job(engine, job["id"])
    result = engine.get(job["id"])
    assert result["status"] == "cancelled"
    assert result["outputs"] == []
    assert fake.views == []


def test_cancel_during_history_error_stays_cancelled(configured):
    engine, fake = configured
    job = engine.submit(comfy.ImageRequest(prompt="running", seed=3))

    def fail_after_cancel(_):
        engine.cancel(job["id"])
        raise comfy.ComfyError("connection closed")

    fake.history = fail_after_cancel
    run_job(engine, job["id"])
    result = engine.get(job["id"])
    assert result["status"] == "cancelled"
    assert "error" not in result


def test_cancel_during_final_poll_sleep_wins_over_timeout(tmp_path):
    mapping = config_mapping(tmp_path)
    mapping.update(poll_interval=0.03, poll_timeout=0.015)
    fake = FakeComfy()
    fake.history = lambda _: {}
    engine = comfy.ComfyImageEngine(comfy.ComfyConfig.from_mapping(mapping), fake)
    job = engine.submit(comfy.ImageRequest(prompt="running", seed=3))
    engine.pending.get_nowait()
    engine.pending.task_done()
    engine.update(job["id"], status="running", prompt_ids=["remote"])

    def cancel_during_sleep():
        time.sleep(0.01)
        engine.update(job["id"], cancel=True)

    cancellation = threading.Thread(target=cancel_during_sleep)
    cancellation.start()
    result = engine._wait_prompt(job["id"], "remote", comfy.ImageRequest(prompt="running", seed=3), 3, 0, time.monotonic())
    cancellation.join()

    assert result == "cancelled"
    assert engine.get(job["id"])["status"] == "cancelled"
    assert "error" not in engine.get(job["id"])


def test_reference_dimensions_are_checked_before_decode(monkeypatch):
    class Oversized:
        format = "PNG"
        width = 5000
        height = 4000

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def load(self):
            raise AssertionError("oversized image must not be decoded")

    monkeypatch.setattr(comfy.Image, "open", lambda _: Oversized())
    with pytest.raises(ValueError, match="16 megapixels"):
        comfy._decode_reference(data_url())


def test_no_duplicate_publish(configured):
    engine, fake = configured
    job = engine.submit(comfy.ImageRequest(prompt="one", seed=1))
    spec = comfy.ImageRequest(prompt="one", seed=1)
    outputs = {"save": {"images": [{"filename": "same.png"}]}}
    engine._publish_outputs(job["id"], spec, 1, "remote", outputs, 0)
    engine._publish_outputs(job["id"], spec, 1, "remote", outputs, 0)
    assert len(engine.get(job["id"])["outputs"]) == 1
    assert len(fake.views) == 1


def test_api_health_and_gallery_basics(configured, monkeypatch):
    engine, _ = configured
    image = engine.library.publish(Image.new("RGBA", (8, 8)), "saved.png", {"seed": 9})
    monkeypatch.setattr(comfy, "engine", engine)
    client = TestClient(comfy.app)
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["controls"]["max_references"] == 1
    assert client.get("/gallery").json()["items"][0]["id"] == image["id"]
    assert client.get("/files/saved.png").status_code == 200
    trash = client.post("/gallery/trash", json={"ids": [image["id"]]}).json()
    assert trash["ok"] is True
    assert client.get("/files/saved.png").status_code == 404
    token = trash["results"][0]["trash_id"]
    assert client.post("/gallery/restore", json={"ids": [token]}).json()["ok"] is True
    assert client.get("/files/saved.png").status_code == 200


def test_health_advertises_only_configured_advanced_modes(tmp_path, monkeypatch):
    basic = comfy.ComfyImageEngine(
        comfy.ComfyConfig.from_mapping(config_mapping(tmp_path), tmp_path / "basic.json"), FakeComfy())
    monkeypatch.setattr(comfy, "engine", basic)
    basic_health = TestClient(comfy.app).get("/health").json()
    assert not ({"transparent_png", "subject_extraction", "mask_edit", "annotation_edit"}
                & set(basic_health["capabilities"]))
    basic_response = TestClient(comfy.app).post("/jobs", json={"prompt": "icon", "mode": "transparent"})
    assert basic_response.status_code == 422
    assert "not enabled" in basic_response.json()["detail"]

    advanced = comfy.ComfyImageEngine(
        comfy.ComfyConfig.from_mapping(advanced_config_mapping(tmp_path), tmp_path / "advanced.json"), FakeComfy())
    monkeypatch.setattr(comfy, "engine", advanced)
    health = TestClient(comfy.app).get("/health").json()
    assert {"transparent_png", "transparent_edit", "subject_extraction", "mask_edit", "annotation_edit"} \
        <= set(health["capabilities"])
    assert health["controls"]["max_references_by_mode"]["masked"] == 1
