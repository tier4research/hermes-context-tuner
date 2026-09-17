from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3

# Bounded sidecar: never store transcript bodies, only metadata and hashes.
MAX_JSON_BLOB_CHARS = 8_000
MAX_POINTER_ROWS_PER_EVENT = 10_000
MAX_SUMMARY_CHARS = 500

_EVENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("operation_id", "TEXT"),
    ("status", "TEXT NOT NULL DEFAULT 'finalized'"),
    ("started_at", "REAL"),
    ("finalized_at", "REAL"),
    ("conversation_id", "TEXT"),
    ("trigger_json", "TEXT"),
    ("pre_count", "INTEGER"),
    ("post_count", "INTEGER"),
    ("pre_tokens", "INTEGER"),
    ("post_tokens", "INTEGER"),
    ("threshold_tokens", "INTEGER"),
    ("tail_budget", "INTEGER"),
    ("protected_head", "INTEGER"),
    ("source_fingerprint", "TEXT"),
    ("result_fingerprint", "TEXT"),
    ("summary_marker", "TEXT"),
    ("checkpoint_required", "INTEGER NOT NULL DEFAULT 0"),
    ("checkpoint_reason", "TEXT"),
)

_POINTER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("range_start", "INTEGER"),
    ("range_end", "INTEGER"),
    ("fingerprint", "TEXT"),
)

VALID_STATUSES = ("started", "finalized", "failed", "aborted", "interrupted")
# From any state, an explicit terminal transition is allowed; from a terminal
# state nothing may change except idempotent re-assertion of the same value.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "started": {"finalized", "failed", "aborted", "interrupted"},
    "finalized": set(),
    "failed": {"interrupted", "aborted"},
    "aborted": set(),
    "interrupted": set(),
}


class SchemaUnsupported(RuntimeError):
    """The store's schema version is newer than this code supports."""


def message_fingerprint(message: dict[str, Any]) -> str:
    """Stable content hash of a message; body itself is never persisted."""
    try:
        canonical = json.dumps(
            message, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        )
    except Exception:
        canonical = repr(message)[:MAX_JSON_BLOB_CHARS]
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()


def _coerce_bound(value: Any, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return int(default)
    return n if n >= 0 else 0


def _fingerprint_chain(fingerprints: list[str]) -> str:
    """Order-sensitive digest over per-message fingerprints.

    Content-covering: the chain changes whenever any source message changes,
    unlike a digest over counts/token totals. Only the digest is persisted.
    """
    digest = hashlib.sha256()
    for fingerprint in fingerprints:
        digest.update(str(fingerprint).encode("utf-8", "replace"))
        digest.update(b"\x00")
    return digest.hexdigest()


class RecoveryPointerStore:
    """Append-mostly SQLite sidecar for compression lineage metadata.

    Stores decision metadata, ranges, and content fingerprints — never raw
    message bodies, credentials, or reasoning text. All lifecycle transitions
    are explicit; historical rows are preserved across migrations.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    # ------------------------------------------------------------------ #
    # connection / schema
    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        # timeout + busy_timeout + WAL: the gateway and concurrent cron
        # sessions share this file; a short lock wait caused spurious
        # "database is locked" failures and silent audit loss.
        con = sqlite3.connect(str(self.path), timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass  # read-only or exotic filesystem: keep default journal
        return con

    def _migrate(self) -> None:
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            version = int(con.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise SchemaUnsupported(
                    f"recovery store schema version {version} is newer than"
                    f" supported version {SCHEMA_VERSION}; refusing to rewrite."
                    " Restore a backup or upgrade hermes-context-tuner."
                )

            if version < 1:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS compression_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at REAL NOT NULL,
                        old_session_id TEXT,
                        new_session_id TEXT,
                        original_count INTEGER NOT NULL,
                        compressed_count INTEGER NOT NULL,
                        total_tokens INTEGER NOT NULL,
                        summary TEXT NOT NULL
                    )
                    """
                )
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recovery_pointers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id INTEGER NOT NULL,
                        source_session_id TEXT,
                        message_index INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        rough_tokens INTEGER NOT NULL,
                        decision TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        FOREIGN KEY(event_id) REFERENCES compression_events(id)
                    )
                    """
                )
                con.execute("PRAGMA user_version = 1")

            if version < 2:
                # v2 semantics: lifecycle columns, backfilled as finalized for
                # pre-existing rows (they were recorded post-commit by v1 code).
                cols = self._columns(con, "compression_events")
                for name, ddl in (
                    ("operation_id", "TEXT"),
                    ("status", "TEXT NOT NULL DEFAULT 'finalized'"),
                    ("finalized_at", "REAL"),
                ):
                    if name not in cols:
                        con.execute(
                            f"ALTER TABLE compression_events ADD COLUMN {name} {ddl}"
                        )
                con.execute(
                    "UPDATE compression_events"
                    " SET status = 'finalized'"
                    " WHERE status IS NULL OR status = ''"
                )
                con.execute("PRAGMA user_version = 2")

            if version < 3:
                cols = self._columns(con, "compression_events")
                for name, ddl in _EVENT_COLUMNS:
                    if name not in cols:
                        con.execute(
                            f"ALTER TABLE compression_events ADD COLUMN {name} {ddl}"
                        )
                # normalize lifecycle naming: legacy branch code wrote
                # 'pending'; live rows are already 'finalized'.
                con.execute(
                    "UPDATE compression_events SET status = 'started'"
                    " WHERE status = 'pending'"
                )
                # backfill lifecycle timestamps where obviously derivable
                con.execute(
                    "UPDATE compression_events"
                    " SET started_at = COALESCE(started_at, created_at),"
                    "     finalized_at = COALESCE(finalized_at, created_at)"
                    " WHERE status = 'finalized'"
                )
                pcols = self._columns(con, "recovery_pointers")
                for name, ddl in _POINTER_COLUMNS:
                    if name not in pcols:
                        con.execute(
                            f"ALTER TABLE recovery_pointers ADD COLUMN {name} {ddl}"
                        )
                # populate ranges/fingerprints from legacy message_index rows
                con.execute(
                    "UPDATE recovery_pointers"
                    " SET range_start = COALESCE(range_start, message_index),"
                    "     range_end = COALESCE(range_end, message_index + 1)"
                )
                con.execute("PRAGMA user_version = 3")

            # operation_id uniqueness enforced at the DB level on both fresh
            # and migrated DBs. NULLs are distinct in SQLite unique indexes,
            # so legacy rows with NULL operation_id remain valid.
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS"
                " idx_compression_events_operation_id"
                " ON compression_events(operation_id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_compression_events_status"
                " ON compression_events(status)"
            )

            # Legacy branch-era schema_info table: keep it agreeable but
            # user_version is the carrier of truth from here on.
            try:
                info = con.execute(
                    "SELECT version FROM schema_info LIMIT 1"
                ).fetchone()
                if info is not None and int(info[0]) != SCHEMA_VERSION:
                    con.execute(
                        "UPDATE schema_info SET version = ?", (SCHEMA_VERSION,)
                    )
            except sqlite3.OperationalError:
                pass

            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()

    @staticmethod
    def _columns(con: sqlite3.Connection, table: str) -> set[str]:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _valid_status(value: Any) -> bool:
        return isinstance(value, str) and value in VALID_STATUSES

    # ------------------------------------------------------------------ #
    # lifecycle API
    # ------------------------------------------------------------------ #
    def begin_compression(
        self,
        *,
        old_session_id: str = "",
        conversation_id: str | None = None,
        original_count: int = 0,
        total_tokens: int = 0,
        messages: list[dict[str, Any]] | None = None,
        decisions: list[dict[str, Any]] | None = None,
        trigger: dict[str, Any] | None = None,
        threshold_tokens: int | None = None,
        tail_budget: int | None = None,
        protected_head: int | None = None,
        source_fingerprint: str | None = None,
    ) -> str:
        """Open a 'started' operation and record pre-state + pointer rows.

        Returns the unique operation_id. Atomic: the event row and its
        pointers are written in one transaction.
        """
        operation_id = uuid.uuid4().hex
        decisions = list(decisions or [])
        trigger_blob = json.dumps(trigger or {}, ensure_ascii=False)[:MAX_JSON_BLOB_CHARS]
        summary = self._summarize_decisions(decisions)
        pointer_rows = self._pointer_rows(
            old_session_id, original_count, messages or [], decisions
        )
        if source_fingerprint is None:
            # Content-covering, order-sensitive source identity: derive it from
            # the same per-message fingerprints the pointer rows persist, so the
            # event-level fingerprint cannot agree while the messages differ.
            source_fingerprint = _fingerprint_chain(
                [row["fingerprint"] for row in pointer_rows]
            )
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                """
                INSERT INTO compression_events
                  (created_at, old_session_id, new_session_id, original_count,
                   compressed_count, total_tokens, summary,
                   operation_id, status, started_at,
                   conversation_id, trigger_json, pre_count, pre_tokens,
                   threshold_tokens, tail_budget, protected_head,
                   source_fingerprint, checkpoint_required)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'started', ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    time.time(),
                    old_session_id,
                    "",  # new_session_id arrives at finalize
                    _coerce_bound(original_count, 0),
                    0,
                    _coerce_bound(total_tokens, 0),
                    summary[:MAX_SUMMARY_CHARS],
                    operation_id,
                    time.time(),
                    conversation_id,
                    trigger_blob,
                    _coerce_bound(original_count, 0),
                    _coerce_bound(total_tokens, 0),
                    threshold_tokens,
                    tail_budget,
                    protected_head,
                    source_fingerprint,
                ),
            )
            event_id = int(cur.lastrowid)
            con.executemany(
                """
                INSERT INTO recovery_pointers
                  (event_id, source_session_id, message_index, range_start,
                   range_end, fingerprint, role, rough_tokens, decision, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        event_id,
                        old_session_id,
                        r["message_index"],
                        r["range_start"],
                        r["range_end"],
                        r["fingerprint"],
                        r["role"],
                        r["rough_tokens"],
                        r["decision"],
                        r["reason"],
                    )
                    for r in pointer_rows
                ],
            )
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            con.close()
            raise
        con.close()
        return operation_id

    def finalize_compression(
        self,
        operation_id: str,
        *,
        new_session_id: str = "",
        compressed_count: int | None = None,
        post_tokens: int | None = None,
        result_fingerprint: str | None = None,
        summary_marker: str | None = None,
        checkpoint_required: bool = False,
        checkpoint_reason: str | None = None,
        conversation_id: str | None = None,
    ) -> bool:
        """Transition started -> finalized (idempotent for repeated calls)."""
        if compressed_count is None:
            raise ValueError("compressed_count is required to finalize")
        extra_sets = {
            "finalized_at": "?",
            "new_session_id": "?",
            "compressed_count": "?",
            "post_count": "?",
            "post_tokens": "?",
            "result_fingerprint": "?",
            "summary_marker": "?",
            "checkpoint_required": "?",
            "checkpoint_reason": "?",
        }
        params: list[Any] = [
            time.time(),
            new_session_id,
            _coerce_bound(compressed_count, 0),
            _coerce_bound(compressed_count, 0),
            post_tokens,
            result_fingerprint,
            (summary_marker or "")[:MAX_SUMMARY_CHARS] or None,
            1 if checkpoint_required else 0,
            (checkpoint_reason or "")[:MAX_SUMMARY_CHARS] or None,
        ]
        # conversation_id arrives with the host boundary callback; only set it
        # when actually supplied so a duplicate finalize cannot null it out.
        if conversation_id is not None:
            extra_sets["conversation_id"] = "?"
            params.append(conversation_id)
        return self._transition(
            operation_id,
            "finalized",
            extra_sets=extra_sets,
            params=tuple(params),
        )

    def fail_compression(
        self,
        operation_id: str,
        *,
        reason: str = "",
    ) -> bool:
        """Transition started -> failed with a bounded reason."""
        return self._transition(
            operation_id,
            "failed",
            extra_sets={"checkpoint_reason": "?"},
            params=((reason or "")[:MAX_SUMMARY_CHARS] or None,),
        )

    def abort_pending(self, operation_id: str | None = None) -> int:
        """Close out 'started' operations as aborted. Returns rows affected.

        With operation_id: transitions that one operation. Without: closes
        EVERY still-open operation — recovery-time use only (e.g. after a
        crash, before new operations begin); it is not safe to sweep-all
        while other sessions may have operations in flight.
        """
        con = self._connect()
        try:
            if operation_id is not None:
                cur = con.execute(
                    "UPDATE compression_events SET status = 'aborted',"
                    " finalized_at = COALESCE(finalized_at, ?)"
                    " WHERE operation_id = ? AND status = 'started'",
                    (time.time(), operation_id),
                )
            else:
                cur = con.execute(
                    "UPDATE compression_events SET status = 'aborted',"
                    " finalized_at = COALESCE(finalized_at, ?)"
                    " WHERE status = 'started'",
                    (time.time(),),
                )
            con.commit()
            return int(cur.rowcount)
        finally:
            con.close()

    def mark_interrupted(self, operation_id: str, *, reason: str = "") -> bool:
        """Record a process-death / unknown-completion operation."""
        return self._transition(
            operation_id,
            "interrupted",
            extra_sets={"checkpoint_reason": "?", "finalized_at": "?"},
            params=((reason or "")[:MAX_SUMMARY_CHARS] or None, time.time()),
        )

    def mark_interrupted_if_started(self, operation_id: str, *, reason: str = "") -> bool:
        """State-guarded interrupted transition that never rejects a terminal row.

        Unlike `_transition`, this cannot raise for an operation that has
        already reached a terminal state: only rows still in 'started' move.
        Safe to call from failure paths where the current state is unknown.
        """
        con = self._connect()
        try:
            cur = con.execute(
                "UPDATE compression_events SET status = 'interrupted',"
                " checkpoint_reason = COALESCE(?, checkpoint_reason),"
                " finalized_at = COALESCE(finalized_at, ?)"
                " WHERE operation_id = ? AND status = 'started'",
                (
                    (reason or "")[:MAX_SUMMARY_CHARS] or None,
                    time.time(),
                    operation_id,
                ),
            )
            con.commit()
            return int(cur.rowcount) > 0
        finally:
            con.close()

    def interrupt_stale_started(self, *, max_age_seconds: float) -> int:
        """Reconcile 'started' rows older than `max_age_seconds` as interrupted.

        Age-gated on purpose: a live in-flight operation owned by another
        session or process is never swept, so this is safe to run at engine
        startup as a process-death safety net. Returns rows changed.
        """
        if max_age_seconds <= 0:
            return 0
        cutoff = time.time() - float(max_age_seconds)
        con = self._connect()
        try:
            cur = con.execute(
                "UPDATE compression_events SET status = 'interrupted',"
                " checkpoint_reason = COALESCE(checkpoint_reason, ?),"
                " finalized_at = COALESCE(finalized_at, ?)"
                " WHERE status = 'started'"
                "   AND COALESCE(started_at, created_at) < ?",
                (
                    "stale operation reconciled at engine startup",
                    time.time(),
                    cutoff,
                ),
            )
            con.commit()
            return int(cur.rowcount)
        finally:
            con.close()

    def _transition(
        self,
        operation_id: str,
        target: str,
        *,
        extra_sets: dict[str, str] | None = None,
        params: tuple = (),
    ) -> bool:
        """Status transition under BEGIN IMMEDIATE with transition validation.

        Idempotent for repeated identical terminal transitions. Invalid
        transitions raise ValueError without partial writes.
        """
        con = self._connect()
        try:
            try:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute(
                    "SELECT status FROM compression_events WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown operation_id: {operation_id}")
                current = row[0] or ""
                if current == target:
                    con.execute("COMMIT")
                    return False  # idempotent no-op
                allowed = _ALLOWED_TRANSITIONS.get(current, set())
                if target not in allowed:
                    raise ValueError(
                        f"invalid status transition {current!r} -> {target!r}"
                        f" for operation {operation_id}"
                    )
                sets = ["status = ?"]
                values: list[Any] = [target]
                for col, placeholder in (extra_sets or {}).items():
                    sets.append(f"{col} = {placeholder}")
                values.extend(params)
                values.append(operation_id)
                con.execute(
                    f"UPDATE compression_events SET {', '.join(sets)}"
                    " WHERE operation_id = ?",
                    values,
                )
                con.execute("COMMIT")
                return True
            except BaseException:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        finally:
            con.close()

    # ------------------------------------------------------------------ #
    # compatibility shim (v1 API used by tests and older callers)
    # ------------------------------------------------------------------ #
    def record_event(
        self,
        *,
        old_session_id: str = "",
        new_session_id: str = "",
        original_count: int,
        compressed_count: int,
        total_tokens: int,
        decisions: list[dict[str, Any]],
    ) -> int:
        """One-shot legacy write: begin + finalize + return event id."""
        op = self.begin_compression(
            old_session_id=old_session_id,
            original_count=original_count,
            total_tokens=total_tokens,
            messages=[{} for _ in range(max(0, int(original_count)))],
            decisions=decisions,
        )
        self.finalize_compression(
            op,
            new_session_id=new_session_id,
            compressed_count=compressed_count,
        )
        con = self._connect()
        try:
            row = con.execute(
                "SELECT id FROM compression_events WHERE operation_id = ?",
                (op,),
            ).fetchone()
            return int(row[0])
        finally:
            con.close()

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #
    def latest_events(self, limit: int = 5) -> list[dict[str, Any]]:
        con = self._connect()
        try:
            rows = con.execute(
                """
                SELECT id, created_at, old_session_id, new_session_id,
                       original_count, compressed_count, total_tokens, summary,
                       operation_id, status, finalized_at, conversation_id,
                       pre_count, post_count, pre_tokens, post_tokens,
                       checkpoint_required, checkpoint_reason,
                       result_fingerprint, summary_marker
                FROM compression_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(0, int(limit)),),
            ).fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]

    def latest_pending(self) -> dict[str, Any] | None:
        con = self._connect()
        try:
            row = con.execute(
                """
                SELECT id, operation_id, status, created_at, started_at,
                       old_session_id, original_count, total_tokens
                FROM compression_events
                WHERE status = 'started'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        finally:
            con.close()
        return dict(row) if row else None

    def event_pointers(self, event_id: int, limit: int = 200) -> list[dict[str, Any]]:
        con = self._connect()
        try:
            rows = con.execute(
                """
                SELECT id, message_index, range_start, range_end, role,
                       rough_tokens, decision, reason, fingerprint
                FROM recovery_pointers
                WHERE event_id = ?
                ORDER BY id
                LIMIT ?
                """,
                (int(event_id), max(1, int(limit))),
            ).fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]

    def verify_pointers(
        self, operation_id: str, source_messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Join pointers against source messages; available iff the source
        message at range_start still fingerprints identically."""
        con = self._connect()
        try:
            rows = con.execute(
                """
                SELECT p.id, p.range_start, p.range_end, p.fingerprint,
                       p.decision, p.reason
                FROM recovery_pointers p
                JOIN compression_events e ON e.id = p.event_id
                WHERE e.operation_id = ?
                ORDER BY p.id
                """,
                (operation_id,),
            ).fetchall()
        finally:
            con.close()
        out: list[dict[str, Any]] = []
        for r in rows:
            start = r["range_start"]
            available = False
            if start is not None and 0 <= int(start) < len(source_messages):
                available = (
                    message_fingerprint(source_messages[int(start)]) == r["fingerprint"]
                )
            out.append(
                {
                    "id": r["id"],
                    "range_start": r["range_start"],
                    "range_end": r["range_end"],
                    "decision": r["decision"],
                    "reason": r["reason"],
                    "available": available,
                }
            )
        return out

    def status_counts(self) -> dict[str, int]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT status, COUNT(*) AS n FROM compression_events GROUP BY status"
            ).fetchall()
        finally:
            con.close()
        return {str(r[0]): int(r[1]) for r in rows}

    def close(self) -> None:
        """No persistent handle is held; retained for API completeness."""

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pointer_rows(
        old_session_id: str,
        original_count: int,
        messages: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for d in decisions[:MAX_POINTER_ROWS_PER_EVENT]:
            idx = d.get("index", d.get("range_start", -1))
            try:
                idx_int = int(idx)
            except (TypeError, ValueError):
                idx_int = -1
            if "range_end" in d:
                try:
                    end = int(d["range_end"])
                except (TypeError, ValueError):
                    end = idx_int + 1
            else:
                end = idx_int + 1
            fingerprint = str(d.get("fingerprint") or "")
            if not fingerprint and 0 <= idx_int < len(messages):
                fingerprint = message_fingerprint(messages[idx_int])
            rows.append(
                {
                    "message_index": idx_int,
                    "range_start": idx_int,
                    "range_end": max(end, idx_int) if end >= idx_int else idx_int + 1,
                    "fingerprint": fingerprint,
                    "role": str(d.get("role", ""))[:64],
                    "rough_tokens": _coerce_bound(d.get("rough_tokens", 0), 0),
                    "decision": str(d.get("decision", ""))[:64],
                    "reason": str(d.get("reason", ""))[:200],
                }
            )
        return rows

    @staticmethod
    def _summarize_decisions(decisions: list[dict[str, Any]]) -> str:
        counts: dict[str, int] = {}
        for d in decisions:
            key = str(d.get("decision", "unknown"))
            counts[key] = counts.get(key, 0) + 1
        return (
            ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no decisions"
        )[:MAX_SUMMARY_CHARS]
