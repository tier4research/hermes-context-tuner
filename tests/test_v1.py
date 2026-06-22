import sqlite3
import pytest
from hermes_context_tuner.engine import ContextTunerEngine, DelegateCompressionError, _invoke_compatible
from hermes_context_tuner.recovery import RecoveryPointerStore

def test_delegate_body_typeerror_is_not_retried():
    calls=[]
    def fn(messages, current_tokens=None): calls.append(1); raise TypeError("body")
    with pytest.raises(TypeError): _invoke_compatible(fn,[(([{}],),{"current_tokens":1}),(([{}],),{})])
    assert calls == [1]

def test_alternate_signature_is_bound():
    def fn(messages): return messages
    assert _invoke_compatible(fn,[(([{}],),{"current_tokens":1}),(([{}],),{})]) == [{}]

def test_two_phase_lineage_and_no_message_content(tmp_path):
    path=tmp_path/"r.db"; store=RecoveryPointerStore(path); secret="credential-super-secret"
    messages=[{"role":"user","content":secret}]
    op=store.begin(old_session_id="old",original_count=1,total_tokens=2,messages=messages,decisions=[{"index":0,"role":"user","decision":"summarize","reason":"budget"}])
    assert store.latest_events()[0]["status"] == "pending"
    assert not store.lookup(op,[])[0]["available"]
    assert store.lookup(op,messages)[0]["available"]
    store.finalize(op,new_session_id="new",compressed_count=1)
    assert store.latest_events()[0]["status"] == "finalized"
    assert secret.encode() not in path.read_bytes()

def test_audit_failure_does_not_break_compression(monkeypatch):
    engine=ContextTunerEngine.__new__(ContextTunerEngine)
    class Delegate:
        threshold_tokens=100; tail_token_budget=10; protect_first_n=1
        def compress(self,messages,current_tokens=None,focus_topic=None): return messages
    class BadStore:
        def begin(self,**kwargs): raise sqlite3.Error("disk")
    engine._delegate=Delegate(); engine._store=BadStore(); engine._last_plan=None; engine._session_id="s"; engine._pending_operation=None; engine._pending_count=0; engine._health={"audit_failures":0,"recovery_failures":0}
    assert engine.compress([{"role":"user","content":"x"}],current_tokens=1)
    assert engine.get_status()["health"]["audit_failures"] == 1
