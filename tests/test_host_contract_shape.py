"""In-repo pin of the Hermes context-engine host contract.

`agent_init`, `conversation_compression`, and `tool_executor` call into the
engine through a fixed shape: they inspect `compress` for the optional kwargs
they may forward, pass `conversation_id` / `old_session_id` / `platform` to
`on_session_start`, pass an extra `messages=` kwarg to `handle_tool_call`, and
wrap each entry of `get_tool_schemas()` as `{"type": "function", "function":
schema}`.

These tests pin that shape so drift is caught by this repo's own suite without
needing a hermes-agent checkout. The external, non-blocking counterpart lives
in the CI `host-contract` job.
"""
from __future__ import annotations

import inspect
import json

from hermes_context_tuner.engine import ContextTunerEngine

from tests.test_engine_journal import FakeDelegate, make_engine


def test_compress_accepts_the_hosts_optional_compression_kwargs():
    # agent/conversation_compression.py::_supported_compression_kwargs inspects
    # this signature and forwards only what it accepts.
    params = inspect.signature(ContextTunerEngine.compress).parameters
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    assert "current_tokens" in params
    assert "focus_topic" in params


def test_host_compression_kwargs_reach_the_delegate(tmp_path):
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)
    engine._session_id = "s-old"
    engine.compress(
        [{"role": "user", "content": "x"}],
        current_tokens=1_234,
        focus_topic="ship it",
        force=True,
        memory_context="provider summary",
        bypass_cooldown=True,
    )
    call = delegate.compress_calls[0]
    assert call["current_tokens"] == 1_234
    assert call["focus_topic"] == "ship it"
    assert call["kw"]["force"] is True
    assert call["kw"]["memory_context"] == "provider summary"
    assert call["kw"]["bypass_cooldown"] is True


def test_session_start_accepts_the_hosts_boundary_kwargs(tmp_path):
    delegate = FakeDelegate()
    engine = make_engine(tmp_path, delegate)
    params = inspect.signature(ContextTunerEngine.on_session_start).parameters
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    engine.on_session_start(
        "s-new",
        boundary_reason="compression",
        old_session_id="s-old",
        platform="telegram",
        conversation_id="gw-key",
    )
    assert delegate.session_starts[0]["boundary_reason"] == "compression"


def test_tool_schemas_are_inner_function_schemas():
    schemas = ContextTunerEngine.get_tool_schemas(object.__new__(ContextTunerEngine))
    assert schemas
    for schema in schemas:
        # agent_init wraps each entry; a pre-wrapped entry would have no
        # top-level name and strict providers reject the malformed tool.
        assert "name" in schema and "parameters" in schema
        assert "function" not in schema
        assert schema["parameters"]["type"] == "object"


def test_handle_tool_call_accepts_the_hosts_messages_kwarg(tmp_path):
    engine = make_engine(tmp_path, FakeDelegate())
    raw = engine.handle_tool_call(
        "context_tuner_status", {}, messages=[{"role": "user", "content": "x"}]
    )
    assert json.loads(raw)["status"]["engine"] == "context-tuner"


def test_lifecycle_hooks_exist_for_the_host():
    for name in (
        "update_from_response",
        "should_compress",
        "compress",
        "on_session_start",
        "on_session_end",
        "on_session_reset",
        "get_status",
        "get_tool_schemas",
        "handle_tool_call",
    ):
        assert callable(getattr(ContextTunerEngine, name, None)), name


def test_engine_passes_through_host_read_state_fields():
    engine = ContextTunerEngine.__new__(ContextTunerEngine)
    delegate = FakeDelegate()
    delegate.context_length = 131_072
    engine._delegate = delegate
    assert engine.context_length == 131_072
    assert engine.name == "context-tuner"


def test_host_state_writes_land_on_the_delegate_not_the_wrapper():
    engine = ContextTunerEngine.__new__(ContextTunerEngine)
    delegate = FakeDelegate()
    engine._delegate = delegate
    engine.model_thresholds = {"grok": 0.5}
    engine._last_compress_refused_would_grow = True
    engine._compression_telemetry_seed = {"n": 1}
    assert delegate.model_thresholds == {"grok": 0.5}
    assert delegate._last_compress_refused_would_grow is True
    assert delegate._compression_telemetry_seed == {"n": 1}
    assert "model_thresholds" not in engine.__dict__
    assert "_compression_telemetry_seed" not in engine.__dict__
    assert engine.model_thresholds == {"grok": 0.5}


def test_update_model_forwards_max_tokens_without_retrying_the_body():
    class Delegate:
        def __init__(self):
            self.calls = []

        def update_model(self, model, context_length, base_url="", api_key="", provider="", api_mode="", max_tokens=None):
            self.calls.append(max_tokens)
            if max_tokens == 0:
                raise TypeError("body error must not be retried")

    engine = ContextTunerEngine.__new__(ContextTunerEngine)
    delegate = Delegate()
    engine._delegate = delegate
    engine.update_model("m", 1000, max_tokens=4096)
    assert delegate.calls == [4096]
    try:
        engine.update_model("m", 1000, max_tokens=0)
    except TypeError as exc:
        assert "body error" in str(exc)
    else:
        raise AssertionError("body TypeError was swallowed")
    assert delegate.calls == [4096, 0]


def test_update_model_omits_max_tokens_when_the_delegate_lacks_it():
    class Narrow:
        def __init__(self):
            self.calls = []

        def update_model(self, model, context_length, base_url="", api_key="", provider="", api_mode=""):
            self.calls.append((model, context_length))

    engine = ContextTunerEngine.__new__(ContextTunerEngine)
    delegate = Narrow()
    engine._delegate = delegate
    engine.update_model("m", 2048, max_tokens=1024)
    assert delegate.calls == [("m", 2048)]
