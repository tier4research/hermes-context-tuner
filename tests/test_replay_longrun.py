"""Long-run replay verification (three-compression deterministic replay).

Fixture: explicit constraints + decisions + unresolved work + artifact refs,
then three compressions with a real Hermes-shaped boundary callback between
each. Verifies: checkpoint fields survive exactly, no semantic claims without
a checkpoint, every summarized range has a pointer, failed attempts are not
reported as committed, resume packet stays under its hard cap, latency and
sidecar growth are measured, and a restart/new session retrieves state
without the transcript.
"""
from __future__ import annotations

import json
import time

from hermes_context_tuner.checkpoints import CheckpointLedger
from hermes_context_tuner.engine import RESUME_PACKET_CAP_BYTES

from tests.test_engine_journal import FakeDelegate, make_engine

CONSTRAINTS = ["constraint-alpha-must-survive", "constraint-beta-also-survives"]
DECISIONS = ["decision-use-sqlite-user-version"]
UNRESOLVED = ["independent-review-pending"]
NEXT_ACTIONS = ["verify-replay-green"]
ARTIFACT = {"path": "src/hermes_context_tuner/recovery.py", "sha256": "b" * 64}


def test_three_compression_replay(tmp_path):
    led = CheckpointLedger(tmp_path / "cp.sqlite")

    # explicit checkpoint BEFORE any compression (rule 6: semantic state
    # enters only through the checkpoint API, never inferred from prose)
    cid = led.write_checkpoint(
        {
            "project_key": "replay",
            "goal": "survive three compressions with state intact",
            "current_stage": "replay-run",
            "status": "in_progress",
            "hard_constraints": CONSTRAINTS,
            "locked_decisions": DECISIONS,
            "unresolved": UNRESOLVED,
            "next_actions": NEXT_ACTIONS,
            "artifacts": [ARTIFACT],
            "provenance": "deterministic replay fixture",
        }
    )

    engine = make_engine(tmp_path, FakeDelegate(result=1))
    engine._ledger = led
    latencies: list[float] = []
    ops: list[str] = []

    big = [{"role": "user", "content": "payload-" + "x" * 200}] * 40
    for i in range(3):
        engine._session_id = f"session-{i}"
        t0 = time.perf_counter()
        engine.compress(big, current_tokens=60_000 + i * 5_000)
        t1 = time.perf_counter()
        latencies.append(t1 - t0)
        assert engine._store.status_counts().get("started") == 1
        # Hermes commits the child session, then fires the boundary callback
        engine.on_session_start(
            f"session-{i + 1}",
            boundary_reason="compression",
            old_session_id=f"session-{i}",
            conversation_id="replay-conv",
        )
        ops.append(engine._store.latest_events(1)[0]["operation_id"])
        # between compressions the resumed packet stays bounded
        packet = engine._build_resume_packet("replay", new_session_id=f"session-{i + 1}")
        assert len(json.dumps(packet).encode()) <= RESUME_PACKET_CAP_BYTES

    counts = engine._store.status_counts()
    assert counts.get("finalized") == 3
    assert counts.get("failed", 0) == 0 and counts.get("started", 0) == 0
    assert len(set(ops)) == 3, "each compression must be a distinct operation"

    # every summarized/externalized range has a pointer
    for i, op in enumerate(ops):
        rows = engine._store.verify_pointers(op, big)
        assert rows, f"compression {i} has no pointers"
        assert all(r["available"] for r in rows)

    # no semantic claim appears unless supplied by the checkpoint: the store
    # contains only counts/roles/reasons from the budget plan, never the
    # fixture's semantic strings
    raw_db = (tmp_path / "engine.sqlite").read_bytes()
    for s in CONSTRAINTS + DECISIONS + UNRESOLVED + NEXT_ACTIONS:
        assert s.encode() not in raw_db, f"semantic claim '{s}' leaked into lineage store"

    # failed attempt is NOT reported as committed
    engine._session_id = "session-3"
    try:
        engine.compress(big, current_tokens=80_000)
    except Exception:
        pass
    bad = FakeDelegate(fail=True)
    engine._delegate = bad
    try:
        engine.compress(big, current_tokens=80_000)
    except RuntimeError:
        pass
    counts = engine._store.status_counts()
    assert counts.get("failed") == 1
    assert counts.get("finalized") == 3, "failed attempt must not become finalized"

    # sidecar growth measured and bounded-ish; latency measured
    import sqlite3

    con = sqlite3.connect(str(engine._store.path))
    n_pointers = con.execute("SELECT COUNT(*) FROM recovery_pointers").fetchone()[0]
    con.close()
    db_bytes = (tmp_path / "engine.sqlite").stat().st_size
    print(
        f"replay metrics: latency={[f'{l:.4f}s' for l in latencies]} "
        f"pointers={n_pointers} lineage_db_bytes={db_bytes}"
    )
    assert max(latencies) < 2.0, "compression+audit latency unreasonable"
    assert db_bytes < 5_000_000, "lineage sidecar growth unreasonable"

    # restart: new engine instances over the same stores retrieve the state
    fresh_store_engine = make_engine(tmp_path, FakeDelegate())
    fresh_store_engine._ledger = CheckpointLedger(tmp_path / "cp.sqlite")
    packet = fresh_store_engine._build_resume_packet("replay", new_session_id="brand-new")
    cp = packet["checkpoint"]
    assert cp["checkpoint_id"] == cid
    assert cp["hard_constraints"] == CONSTRAINTS
    assert cp["locked_decisions"] == DECISIONS
    assert cp["unresolved"] == UNRESOLVED
    assert cp["next_actions"] == NEXT_ACTIONS
    assert cp["artifacts"] == [ARTIFACT]
    assert packet["new_session_id"] == "brand-new"
    # last compression pointer present and honest
    assert packet["last_compression"]["status"] in ("finalized", "failed")
