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


def png_data(color=(40, 80, 120, 255)):
    stream = io.BytesIO()
    Image.new("RGBA", (16, 12), color).save(stream, format="PNG")
    return stream.getvalue()


def data_url(data=None):
    return "data:image/png;base64," + base64.b64encode(data or png_data()).decode()


def workflow(include_image=False):
    nodes = {
        "prompt": {"class_type": "CLIPTextEncode", "inputs": {"text": "template prompt"}},
        "negative": {"class_type": "CLIPTextEncode", "inputs": {"text": "template negative"}},
        "seed": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 30, "cfg": 1.0}},
        "size": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024}},
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
