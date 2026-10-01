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
        self.interrupted_ids = []
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

    def interrupt(self, prompt_id):
        self.interrupts += 1
        self.interrupted_ids.append(prompt_id)


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

    assert comfy._decode_mask(data_url(png_data((0, 0, 0, 255))), reference)
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


def test_running_cancel_targets_comfy_prompt_and_waits_for_acknowledgement(configured):
    engine, fake = configured
    job = engine.submit(comfy.ImageRequest(prompt="running", seed=3))
    engine.update(job["id"], status="running", prompt_ids=["remote"])
    fake.queue = lambda: {"queue_running": [[1, "remote"]], "queue_pending": []}
    result = engine.cancel(job["id"])
    assert result["status"] == "cancelling"
    assert fake.interrupted_ids == ["remote"]
    engine._cancel_remote(job["id"], "remote")
    assert fake.interrupted_ids == ["remote"]
    assert engine.get(job["id"])["status"] == "cancelling"
    fake.queue = lambda: {"queue_running": [[2, "someone-else"]], "queue_pending": []}
    assert engine._cancel_remote(job["id"], "remote") is True
    assert engine.get(job["id"])["status"] == "cancelled"
    assert fake.interrupted_ids == ["remote"]


@pytest.mark.parametrize("version,supported", [("0.37.0", True), ("0.38.1", True),
                                               ("0.36.0", False), ("", False)])
def test_targeted_interrupt_protocol_never_sends_global_request(monkeypatch, version, supported):
    client = comfy.ComfyClient("http://localhost:8188")
    calls = []

    def request(method, path, payload=comfy._MISSING, **kwargs):
        calls.append((method, path, payload))
        return {"system": {"comfyui_version": version}} if path == "/system_stats" else b""

    monkeypatch.setattr(client, "_request", request)
    if supported:
        client.interrupt("mine")
        assert calls[-1] == ("POST", "/api/jobs/mine/cancel", {})
    else:
        with pytest.raises(comfy.ComfyError, match="confirmed targeted"):
            client.interrupt("mine")
        assert all('/cancel' not in path for _, path, _ in calls)
    with pytest.raises(comfy.ComfyError, match="prompt ID"):
        client.interrupt("")


@pytest.mark.parametrize("cancel_on", [1, 2])
def test_cancel_while_posting_prompt_still_interrupts_remote(configured, cancel_on):
    engine, fake = configured
    original_prompt = fake.prompt

    def submit(graph):
        if len(fake.prompts) + 1 == cancel_on:
            assert engine.cancel(job["id"])["status"] == "cancelling"
        return original_prompt(graph)

    running = True

    def remote_queue():
        nonlocal running
        result = {"queue_running": [[1, f"prompt-{cancel_on}"]] if running else [], "queue_pending": []}
        running = False
        return result

    fake.prompt, fake.queue = submit, remote_queue
    job = engine.submit(comfy.ImageRequest(prompt="fox", seed=1, n=cancel_on))
    run_job(engine, job["id"])
    assert fake.interrupted_ids == [f"prompt-{cancel_on}"]
    assert engine.get(job["id"])["status"] == "cancelled"
    assert len(fake.views) == cancel_on - 1


def test_unconfirmed_remote_cancel_is_not_reported_as_cancelled(tmp_path, monkeypatch):
    mapping = config_mapping(tmp_path)
    mapping.update(poll_timeout=0.01)
    fake = FakeComfy()
    fake.queue = lambda: {"queue_running": [[1, "remote"]], "queue_pending": []}
    engine = comfy.ComfyImageEngine(comfy.ComfyConfig.from_mapping(mapping), fake)
    job = engine.submit(comfy.ImageRequest(prompt="fox", seed=1))
    engine.update(job["id"], status="running", prompt_ids=["remote"])
    assert engine.cancel(job["id"])["status"] == "cancelling"
    result = engine._wait_prompt(job["id"], "remote", comfy.ImageRequest(prompt="fox"), 1, 0, time.monotonic())
    assert result == "cancelling"
    assert engine.get(job["id"])["status"] == "cancelling"
    assert engine.get(job["id"])["cancel_confirmation_pending"] is True
    assert fake.interrupted_ids == ["remote"]
    engine.cancel(job["id"])
    assert fake.interrupted_ids == ["remote", "remote"]
    fake.queue = lambda: {"queue_running": [], "queue_pending": []}
    monkeypatch.setattr(comfy, "engine", engine)
    assert TestClient(comfy.app).get('/jobs/' + job['id']).json()['status'] == 'cancelled'


def test_lost_prompt_response_still_has_a_cancellable_remote_id(tmp_path, monkeypatch):
    from contextlib import nullcontext
    client = comfy.ComfyClient("http://localhost:8188")
    monkeypatch.setattr(client, "progress", lambda: nullcontext())
    accepted = None
    inspected = 0
    interrupted = []

    def request(method, path, payload=comfy._MISSING, **kwargs):
        nonlocal accepted, inspected
        if path == '/system_stats':
            return {'system': {'comfyui_version': '0.37.0'}}
        if path == '/prompt':
            accepted = payload['prompt_id']
            assert accepted in engine.get(job['id'])['prompt_ids']
            raise comfy.ComfyError('response lost after acceptance')
        if path == '/queue':
            inspected += 1
            return {'queue_running': [[1, accepted]] if inspected == 1 else [], 'queue_pending': []}
        if path == '/api/jobs/' + str(accepted) + '/cancel':
            interrupted.append(accepted)
            return b''
        raise AssertionError(path)

    monkeypatch.setattr(client, '_request', request)
    engine = comfy.ComfyImageEngine(comfy.ComfyConfig.from_mapping(config_mapping(tmp_path)), client)
    job = engine.submit(comfy.ImageRequest(prompt='fox', seed=1))
    run_job(engine, job['id'])
    assert interrupted == [accepted]
    assert engine.get(job['id'])['status'] == 'error'
    assert 'remote job stopped' in engine.get(job['id'])['stage']
    assert engine.get(job['id'])['outputs'] == []


def test_lost_response_cannot_confirm_cancel_before_remote_acceptance(configured):
    engine, fake = configured
    job = engine.submit(comfy.ImageRequest(prompt='fox', seed=1))
    engine.update(job['id'], status='cancelling', cancel=True, prompt_ids=['mine'],
                  submission_uncertain=True, remote_seen=False)
    fake.history = lambda _: {}
    assert engine._cancel_remote(job['id'], 'mine') is False
    assert engine.get(job['id'])['status'] == 'cancelling'
    fake.queue = lambda: {'queue_running': [[1, 'mine']], 'queue_pending': []}
    assert engine._cancel_remote(job['id'], 'mine') is False
    assert fake.interrupted_ids == ['mine']
    fake.queue = lambda: {'queue_running': [], 'queue_pending': []}
    assert engine._cancel_remote(job['id'], 'mine') is True
    assert engine.get(job['id'])['status'] == 'cancelled'


@pytest.mark.parametrize('legacy', [False, True])
def test_publish_failure_cannot_overwrite_confirmed_cancel(configured, legacy):
    engine, fake = configured
    original_history = fake.history
    if legacy:
        fake.history = lambda prompt_id: {prompt_id: {'outputs': original_history(prompt_id)[prompt_id]['outputs']}}

    def failing_view(*args):
        engine.cancel(job['id'])
        raise comfy.ComfyError('view failed after cancellation')

    fake.view = failing_view
    job = engine.submit(comfy.ImageRequest(prompt='fox', seed=1))
    run_job(engine, job['id'])
    assert engine.get(job['id'])['status'] == 'cancelled'
    assert engine.get(job['id'])['outputs'] == []


def test_pending_cancel_waits_for_queue_removal_and_queue_error_is_not_ack(configured):
    engine, fake = configured
    job = engine.submit(comfy.ImageRequest(prompt="fox", seed=1))
    engine.update(job["id"], status="running", prompt_ids=["mine"])
    fake.queue = lambda: {"queue_running": [[1, "other"]], "queue_pending": [[2, "mine"]]}
    assert engine.cancel(job["id"])["status"] == "cancelling"
    assert fake.deleted == ["mine"]
    assert fake.interrupts == 0
    fake.queue = lambda: {}
    assert engine._cancel_remote(job["id"], "mine") is False
    assert engine.get(job["id"])["status"] == "cancelling"
    assert engine.get(job["id"])["cancel_error"]
    fake.queue = lambda: {"queue_running": [[1, "other"]], "queue_pending": []}
    assert engine._cancel_remote(job["id"], "mine") is True
    assert engine.get(job["id"])["status"] == "cancelled"


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


def test_large_valid_reference_is_not_rejected_by_app_pixel_cap():
    raw = io.BytesIO()
    Image.new("RGBA", (5000, 4000), (0, 0, 0, 255)).save(raw, format="PNG")
    decoded, _ = comfy._decode_reference(data_url(raw.getvalue()))
    with Image.open(io.BytesIO(decoded)) as result:
        assert result.size == (5000, 4000)


def test_no_duplicate_publish(configured):
    engine, fake = configured
    job = engine.submit(comfy.ImageRequest(prompt="one", seed=1))
    spec = comfy.ImageRequest(prompt="one", seed=1)
    outputs = {"save": {"images": [{"filename": "same.png"}]}}
    engine._publish_outputs(job["id"], spec, 1, "remote", outputs, 0)
    engine._publish_outputs(job["id"], spec, 1, "remote", outputs, 0)
    assert len(engine.get(job["id"])["outputs"]) == 1
    assert len(fake.views) == 1


def test_prompt_submits_same_client_id_as_progress_connection(monkeypatch):
    client = comfy.ComfyClient("http://localhost:8188")
    requests = []
    monkeypatch.setattr(client, "_request", lambda *args: requests.append(args) or {"prompt_id": "remote"})
    client.prompt({"node": {}})
    assert requests[0][2]["client_id"] == client.client_id
    assert client.client_id in client.progress().url


def test_real_progress_is_scoped_monotonic_and_subscription_closes(configured):
    engine, fake = configured
    snapshots = []

    class Progress:
        available = True
        opened = closed = False
        turn = 0

        def __enter__(self):
            self.opened = True
            return self

        def __exit__(self, *_):
            self.closed = True

        def events(self):
            self.turn += 1
            data = {"prompt_id": "prompt-1", "node": "seed", "value": 2, "max": 6}
            if self.turn == 1:
                return [{"type": "progress", "data": {**data, "prompt_id": "other"}}]
            if self.turn == 2:
                return [{"type": "progress", "data": data}]
            if self.turn == 3:
                return [{"type": "progress", "data": {**data, "value": 1}}]
            return [{"type": "executing", "data": {"prompt_id": "prompt-1", "node": "save"}}]

    progress = Progress()
    fake.progress = lambda: progress
    original_prompt, original_history = fake.prompt, fake.history

    def prompt(graph):
        assert progress.opened
        return original_prompt(graph)

    def history(prompt_id):
        snapshots.append(engine.get(job["id"]))
        return original_history(prompt_id) if len(snapshots) == 4 else {}

    fake.prompt, fake.history = prompt, history
    job = engine.submit(comfy.ImageRequest(prompt="fox", seed=1, steps=6))
    run_job(engine, job["id"])
    assert [snap["completed_steps"] for snap in snapshots] == [0, 2, 2, 2]
    assert snapshots[1]["sampling_step"] == 2
    assert snapshots[1]["progress_determinate"] is True
    assert snapshots[3]["progress_determinate"] is False
    assert engine.get(job["id"])["status"] == "done"
    assert progress.closed


def test_history_polling_never_fabricates_steps(configured):
    engine, fake = configured
    snapshots = []
    history = fake.history

    def delayed(prompt_id):
        snapshots.append(engine.get(job["id"])["completed_steps"])
        return history(prompt_id) if len(snapshots) == 4 else {}

    fake.history = delayed
    job = engine.submit(comfy.ImageRequest(prompt="fox", seed=1, steps=6))
    run_job(engine, job["id"])
    assert snapshots == [0, 0, 0, 0]
    assert engine.get(job["id"])["status"] == "done"


@pytest.mark.parametrize("cancel", [False, True])
def test_progress_loss_keeps_history_authoritative_and_closes(configured, cancel):
    engine, fake = configured

    class Progress:
        available = False
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.closed = True

        def events(self):
            return []

    progress = Progress()
    fake.progress = lambda: progress
    history = fake.history

    def finish(prompt_id):
        snapshot = engine.get(job["id"])
        assert snapshot["completed_steps"] == 0
        assert snapshot["progress_determinate"] is False
        assert "live progress unavailable" in snapshot["stage"]
        if cancel:
            engine.update(job["id"], cancel=True)
        return history(prompt_id)

    fake.history = finish
    job = engine.submit(comfy.ImageRequest(prompt="fox", seed=1, steps=6))
    run_job(engine, job["id"])
    assert engine.get(job["id"])["status"] == ("cancelled" if cancel else "done")
    assert len(fake.views) == (0 if cancel else 1)
    assert progress.closed


def test_multiple_images_use_actual_sampler_fraction_and_image_offset(configured):
    engine, fake = configured
    snapshots = []

    class Progress:
        available = True

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def events(self):
            count = len(fake.prompts)
            return [{"type": "progress", "data": {"prompt_id": f"prompt-{count}",
                     "node": "seed", "value": 2, "max": 6 if count == 1 else 8}}]

    fake.progress = Progress
    history = fake.history

    def finish(prompt_id):
        snapshots.append(engine.get(job["id"]))
        return history(prompt_id)

    fake.history = finish
    job = engine.submit(comfy.ImageRequest(prompt="fox", seed=1, steps=6, n=2))
    run_job(engine, job["id"])
    assert [snap["completed_steps"] for snap in snapshots] == [2, 7.5]
    assert [snap["sampling_image"] for snap in snapshots] == [1, 2]
    assert all(snap["sampling_images"] == 2 for snap in snapshots)
    assert engine.get(job["id"])["status"] == "done"


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


@pytest.mark.parametrize("source", ["photos", "trash"])
def test_permanent_delete_api_pair_partial_results_and_required_source(configured, monkeypatch, source):
    engine, _ = configured
    monkeypatch.setattr(comfy, "engine", engine)
    client = TestClient(comfy.app)
    image = engine.library.publish(Image.new("RGBA", (8, 8)), "purge.png", {"seed": 9})
    keep = engine.library.publish(Image.new("RGBA", (8, 8)), "keep.png", {"seed": 10})
    identifier = image["id"]
    if source == "trash":
        identifier = engine.library.move_to_trash(identifier)["id"]
    assert client.post("/gallery/purge", json={"ids": [identifier]}).status_code == 422
    result = client.post("/gallery/purge", json={"source": source, "ids": [identifier, identifier, "invalid"]}).json()
    assert len(result["results"]) == 2
    assert result["results"][0]["ok"] and result["results"][0]["state"] == "purged"
    assert not result["results"][1]["ok"] and not result["ok"]
    assert client.get("/files/purge.png").status_code == 404
    assert engine.library.gallery()["items"][0]["id"] == keep["id"]
    assert engine.library.trash_items()["items"] == []
    assert not (engine.library.root / "purge.png.json").exists()


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


def test_generation_seconds_saved_per_variation_and_exposed_in_gallery(configured, monkeypatch):
    engine, fake = configured
    job = engine.submit(comfy.ImageRequest(prompt="one", seed=1, n=2))
    clock = [10.0]
    monkeypatch.setattr(comfy.time, "monotonic", lambda: clock[0])
    original_history = fake.history
    def history(prompt_id):
        if len(fake.prompts) == 2:
            assert engine.get(job["id"])["seconds"] == 2.5
        clock[0] += 2.5 if len(fake.prompts) == 1 else 3.5
        return original_history(prompt_id)
    monkeypatch.setattr(fake, "history", history)
    run_job(engine, job["id"])
    items = engine.library.gallery()["items"]
    assert sorted(item["seconds"] for item in items) == [2.5, 3.5]
    assert engine.get(job["id"])["seconds"] == 6
    for item in items:
        metadata = json.loads((engine.output / (item["name"] + ".json")).read_text())
        assert metadata["seconds"] == item["seconds"]
