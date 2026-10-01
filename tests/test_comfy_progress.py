import json

import pytest

from server.comfy_progress import ComfyProgress, event_updates


GRAPH = {"sample": {"class_type": "SamplerCustomAdvanced"},
         "decode": {"class_type": "VAEDecodeTiled"},
         "encode": {"class_type": "TextEncodeQwenImage21"}}


def event(kind="progress", **data):
    return {"type": kind, "data": {"prompt_id": "mine", "node": "sample", **data}}


def test_prompt_scoping_and_only_sampling_counts():
    assert not event_updates(event(prompt_id="other", value=2, max=6), "mine", GRAPH)
    assert not event_updates(event(node="decode", value=2, max=6), "mine", GRAPH)
    assert not event_updates(event(node="missing", value=2, max=6), "mine", GRAPH)
    update = event_updates(event(value=2, max=6), "mine", GRAPH)[0]
    assert update == {"stage": "Sampling 2 / 6 steps", "progress_determinate": True,
                      "sampling_step": 2, "sampling_steps": 6}
    assert event_updates(event("executing", node="encode"), "mine", GRAPH) == [
        {"stage": "Encoding prompt and references", "progress_determinate": False}]
    assert not event_updates(event("executing", node=None), "mine", GRAPH)


@pytest.mark.parametrize("value,maximum", [(True, 6), (2, 0), (-1, 6), (7, 6),
                                          (float("nan"), 6), (2, float("inf")), (1.5, 6), (2, "6"),
                                          (10 ** 1000, 10 ** 1001), (0, 1)])
def test_malformed_progress_is_ignored(value, maximum):
    assert not event_updates(event(value=value, max=maximum), "mine", GRAPH)


def test_progress_state_ignores_finished_and_decode_nodes():
    update = event_updates(event("progress_state", nodes={
        "sample": {"state": "running", "value": 3, "max": 8},
        "decode": {"state": "running", "value": 2, "max": 10},
        "encode": {"state": "finished", "value": 1, "max": 1},
    }), "mine", GRAPH)
    assert len(update) == 1
    assert update[0]["sampling_step"] == 3
    assert update[0]["sampling_steps"] == 8


def test_socket_frames_disconnect_and_close(monkeypatch):
    from websockets.sync import client

    class Socket:
        closed = False
        frames = iter([b"preview", "invalid", "[]", json.dumps(event(value=2, max=6))])

        def recv(self, timeout):
            assert timeout == 0
            try:
                return next(self.frames)
            except StopIteration:
                raise OSError("disconnected")

        def close(self):
            self.closed = True

    socket = Socket()
    calls = []
    monkeypatch.setattr(client, "connect", lambda url, **kw: calls.append((url, kw)) or socket)
    with ComfyProgress("http://localhost:8188", "id with space") as progress:
        assert progress.available
        assert progress.events() == [event(value=2, max=6)]
        assert not progress.available
    assert socket.closed
    assert calls[0][0] == "ws://localhost:8188/ws?clientId=id+with+space"
    assert calls[0][1]["proxy"] is None


def test_connection_failure_is_optional(monkeypatch):
    from websockets.sync import client

    def fail(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(client, "connect", fail)
    with ComfyProgress("https://example.test", "mine") as progress:
        assert not progress.available
        assert progress.events() == []
