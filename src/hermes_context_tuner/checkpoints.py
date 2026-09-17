from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

ALLOWED_CHECKPOINT_STATUSES = ("in_progress", "blocked", "done", "abandoned")
ALLOWED_TASK_STATUSES = ("in_progress", "blocked", "done", "abandoned")

# Bounded-payload caps (plan Task 11)
MAX_LIST_ITEMS = 100
MAX_STR_CHARS = 1_000
MAX_PAYLOAD_BYTES = 16_000
MAX_NOTE_CHARS = 300
MAX_ARTIFACT_PATH_CHARS = 1_000

REQUIRED_FIELDS = ("project_key", "goal", "current_stage", "status")
LIST_FIELDS = (
    "completed",
    "locked_decisions",
    "hard_constraints",
    "unresolved",
    "next_actions",
)
OPTIONAL_STR_FIELDS = ("task_id", "conversation_id", "provenance")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checkpoint_id TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    project_key TEXT NOT NULL,
    conversation_id TEXT,
    task_id TEXT,
    payload_json TEXT NOT NULL
)
"""
_DECISIONS = """
CREATE TABLE IF NOT EXISTS decision_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    project_key TEXT NOT NULL,
    conversation_id TEXT,
    content TEXT NOT NULL,
    supersedes TEXT,
    source TEXT
)
"""
_TASKS = """
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    project_key TEXT NOT NULL,
    conversation_id TEXT,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT
)
"""


def _require_str(value: Any, field: str, cap: int = MAX_STR_CHARS) -> str:
    """Reject (don't truncate) semantic content that exceeds caps."""
    if not isinstance(value, str):
        raise ValueError(f"checkpoint field '{field}' must be a string")
    if len(value) > cap:
        raise ValueError(
            f"checkpoint field '{field}' too long ({len(value)} > {cap} chars)"
        )
    return value


class CheckpointLedger:
    """Small append-only ledger for explicit project/task state.

    Checkpoints, decision events, and task transitions are insert-only.
    A correction creates a new event with a supersession link; historical
    rows are never edited. The latest project state is a reconstruction,
    not a mutable row.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = self._connect()
        try:
            con.execute(_SCHEMA)
            con.execute(_DECISIONS)
            con.execute(_TASKS)
            con.commit()
        finally:
            con.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        return con

    # ------------------------------------------------------------------ #
    # validation
    # ------------------------------------------------------------------ #
    def _validate_checkpoint(self, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("checkpoint payload must be an object")
        cleaned: dict[str, Any] = {}
        for field in REQUIRED_FIELDS:
            value = data.get(field)
            if field == "status":
                if value not in ALLOWED_CHECKPOINT_STATUSES:
                    raise ValueError(
                        f"checkpoint status must be one of {ALLOWED_CHECKPOINT_STATUSES}"
                    )
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(f"checkpoint field '{field}' is required")
            cleaned[field] = _require_str(value, field)

        cleaned["project_key"] = _require_str(data.get("project_key", ""), "project_key", 200)
        for field in OPTIONAL_STR_FIELDS:
            if data.get(field) is not None:
                cleaned[field] = _require_str(data[field], field)

        for field in LIST_FIELDS:
            value = data.get(field, [])
            if value is None:
                value = []
            if not isinstance(value, list):
                raise ValueError(f"checkpoint field '{field}' must be a list")
            if len(value) > MAX_LIST_ITEMS:
                raise ValueError(
                    f"checkpoint field '{field}' has too many items (>{MAX_LIST_ITEMS})"
                )
            items: list[str] = []
            for v in value:
                item = _require_str(v, field)
                if item.strip():
                    items.append(item)
            cleaned[field] = items

        artifacts = data.get("artifacts", []) or []
        if not isinstance(artifacts, list) or len(artifacts) > MAX_LIST_ITEMS:
            raise ValueError("checkpoint artifacts must be a list of <= 100 items")
        cleaned_artifacts = []
        for a in artifacts:
            if not isinstance(a, dict) or not str(a.get("path", "")).strip():
                raise ValueError("checkpoint artifact requires a 'path'")
            sha = a.get("sha256")
            if sha is not None:
                if not isinstance(sha, str) or len(sha) != 64 or any(
                    c not in "0123456789abcdefABCDEF" for c in sha
                ):
                    raise ValueError("checkpoint artifact sha256 must be a 64-char hex hash")
            entry = {"path": _require_str(a["path"], "artifact path", MAX_ARTIFACT_PATH_CHARS)}
            if sha:
                entry["sha256"] = sha.lower()
            cleaned_artifacts.append(entry)
        cleaned["artifacts"] = cleaned_artifacts

        payload = json.dumps(cleaned, ensure_ascii=False, sort_keys=True)
        if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"checkpoint payload exceeds {MAX_PAYLOAD_BYTES} bytes"
                " — externalize content, don't stuff the ledger"
            )
        return cleaned

    # ------------------------------------------------------------------ #
    # writes (append-only)
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # inserts (shared by single-event and atomic bundle writes)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _insert_checkpoint(con: sqlite3.Connection, cleaned: dict[str, Any]) -> str:
        checkpoint_id = uuid.uuid4().hex
        con.execute(
            "INSERT INTO checkpoints (checkpoint_id, created_at, project_key,"
            " conversation_id, task_id, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                checkpoint_id,
                time.time(),
                cleaned["project_key"],
                cleaned.get("conversation_id"),
                cleaned.get("task_id"),
                json.dumps(cleaned, ensure_ascii=False, sort_keys=True),
            ),
        )
        return checkpoint_id

    @staticmethod
    def _insert_decision(
        con: sqlite3.Connection,
        project_key: str,
        content: str,
        *,
        supersedes: str | None,
        source: str | None,
        conversation_id: str | None,
    ) -> str:
        decision_id = uuid.uuid4().hex
        con.execute(
            "INSERT INTO decision_events (decision_id, created_at, project_key,"
            " conversation_id, content, supersedes, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                decision_id,
                time.time(),
                project_key,
                conversation_id,
                content,
                supersedes,
                source,
            ),
        )
        return decision_id

    @staticmethod
    def _insert_task(
        con: sqlite3.Connection,
        project_key: str,
        task_id: str,
        status: str,
        *,
        note: str | None,
        conversation_id: str | None,
    ) -> None:
        con.execute(
            "INSERT INTO task_events (created_at, project_key, conversation_id,"
            " task_id, status, note) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), project_key, conversation_id, task_id, status, note),
        )

    def write_checkpoint(self, data: dict[str, Any]) -> str:
        """Insert one checkpoint. Thin wrapper over the atomic bundle write."""
        return self.write_checkpoint_bundle(data)["checkpoint_id"]

    def write_checkpoint_bundle(
        self,
        data: dict[str, Any],
        *,
        decisions: list[dict[str, Any]] | None = None,
        task_updates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Validate, then write a checkpoint + decision events + task
        transitions in ONE transaction.

        Every part is validated before anything is written and the writes
        share a single transaction, so a rejected decision or task leaves the
        ledger completely untouched (no partial checkpoint/event state).
        Returns ``{checkpoint_id, decision_ids, tasks_recorded}``.
        """
        cleaned = self._validate_checkpoint(data)
        decisions = list(decisions or [])
        task_updates = list(task_updates or [])
        if len(decisions) > MAX_LIST_ITEMS or len(task_updates) > MAX_LIST_ITEMS:
            raise ValueError("checkpoint bundle has too many decision/task entries")

        prepared_decisions: list[tuple[str, str | None, str | None]] = []
        for entry in decisions:
            if not isinstance(entry, dict):
                raise ValueError("each decision must be an object")
            content = entry.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("decision content is required")
            supersedes = entry.get("supersedes")
            if supersedes is not None and not isinstance(supersedes, str):
                raise ValueError("decision 'supersedes' must be a string")
            source = entry.get("source")
            prepared_decisions.append(
                (
                    _require_str(content, "decision content"),
                    supersedes,
                    _require_str(source, "source") if source else None,
                )
            )

        prepared_tasks: list[tuple[str, str, str | None]] = []
        for entry in task_updates:
            if not isinstance(entry, dict):
                raise ValueError("each task update must be an object")
            task_id = entry.get("task_id")
            status = entry.get("status")
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("task update requires a task_id")
            if status not in ALLOWED_TASK_STATUSES:
                raise ValueError(f"task status must be one of {ALLOWED_TASK_STATUSES}")
            note = entry.get("note")
            prepared_tasks.append(
                (
                    _require_str(task_id, "task_id", 200),
                    status,
                    _require_str(note, "note", MAX_NOTE_CHARS) if note else None,
                )
            )

        conversation_id = cleaned.get("conversation_id")
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            checkpoint_id = self._insert_checkpoint(con, cleaned)
            decision_ids: list[str] = []
            for content, supersedes, source in prepared_decisions:
                if supersedes is not None:
                    row = con.execute(
                        "SELECT 1 FROM decision_events"
                        " WHERE decision_id = ? AND project_key = ?",
                        (supersedes, cleaned["project_key"]),
                    ).fetchone()
                    if row is None:
                        raise ValueError(
                            f"unknown decision_id to supersede: {supersedes}"
                        )
                decision_ids.append(
                    self._insert_decision(
                        con,
                        cleaned["project_key"],
                        content,
                        supersedes=supersedes,
                        source=source,
                        conversation_id=conversation_id,
                    )
                )
            for task_id, status, note in prepared_tasks:
                self._insert_task(
                    con,
                    cleaned["project_key"],
                    task_id,
                    status,
                    note=note,
                    conversation_id=conversation_id,
                )
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()
        return {
            "checkpoint_id": checkpoint_id,
            "decision_ids": decision_ids,
            "tasks_recorded": len(prepared_tasks),
        }

    def append_decision(
        self,
        project_key: str,
        content: str,
        *,
        supersedes: str | None = None,
        source: str | None = None,
        conversation_id: str | None = None,
    ) -> str:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("decision content is required")
        content = _require_str(content, "decision content")
        source = _require_str(source, "source") if source else None
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            if supersedes is not None:
                row = con.execute(
                    "SELECT 1 FROM decision_events WHERE decision_id = ? AND project_key = ?",
                    (supersedes, project_key),
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown decision_id to supersede: {supersedes}")
            decision_id = self._insert_decision(
                con,
                project_key,
                content,
                supersedes=supersedes,
                source=source,
                conversation_id=conversation_id,
            )
            con.execute("COMMIT")
            return decision_id
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()

    def record_task_transition(
        self,
        project_key: str,
        task_id: str,
        status: str,
        *,
        note: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        if status not in ALLOWED_TASK_STATUSES:
            raise ValueError(f"task status must be one of {ALLOWED_TASK_STATUSES}")
        task_id = _require_str(task_id, "task_id", 200)
        note = _require_str(note, "note", MAX_NOTE_CHARS) if note else None
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            self._insert_task(
                con,
                project_key,
                task_id,
                status,
                note=note,
                conversation_id=conversation_id,
            )
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()

    # ------------------------------------------------------------------ #
    # reconstruction
    # ------------------------------------------------------------------ #
    def latest_state(
        self, project_key: str, *, conversation_id: str | None = None
    ) -> dict[str, Any] | None:
        con = self._connect()
        try:
            cp_q = "SELECT * FROM checkpoints WHERE project_key = ?"
            params = [project_key]
            if conversation_id is not None:
                cp_q += " AND conversation_id = ?"
                params.append(conversation_id)
            cp_q += " ORDER BY id DESC LIMIT 1"
            cp = con.execute(cp_q, params).fetchone()

            dec_q = "SELECT * FROM decision_events WHERE project_key = ?"
            dec_params: list[Any] = [project_key]
            if conversation_id is not None:
                dec_q += " AND conversation_id IS ?"
                dec_params.append(conversation_id)
            dec_q += " ORDER BY id"
            decisions = con.execute(dec_q, dec_params).fetchall()

            task_q = "SELECT * FROM task_events WHERE project_key = ?"
            task_params = [project_key]
            if conversation_id is not None:
                task_q += " AND conversation_id IS ?"
                task_params.append(conversation_id)
            task_q += " ORDER BY id"
            tasks = con.execute(task_q, task_params).fetchall()
        finally:
            con.close()

        if cp is None and not decisions and not tasks:
            return None

        checkpoint: dict[str, Any] | None = None
        if cp is not None:
            checkpoint = json.loads(cp["payload_json"])
            checkpoint["checkpoint_id"] = cp["checkpoint_id"]
            checkpoint["created_at"] = cp["created_at"]

        superseded: set[str] = {
            str(r["supersedes"])
            for r in decisions
            if r["supersedes"] is not None
        }
        active = [
            {
                "decision_id": r["decision_id"],
                "content": r["content"],
                "created_at": r["created_at"],
                "supersedes": r["supersedes"],
                "source": r["source"],
            }
            for r in decisions
            if r["decision_id"] not in superseded
        ]

        task_state: dict[str, Any] = {}
        for r in tasks:
            t = task_state.setdefault(
                str(r["task_id"]), {"status": None, "history": []}
            )
            t["status"] = r["status"]
            t["history"].append(
                {"status": r["status"], "note": r["note"], "created_at": r["created_at"]}
            )
        if cp is None and not active and not task_state:
            return None

        return {
            "checkpoint": checkpoint,
            "decisions": active,
            "tasks": task_state,
        }

    def status_counts(self) -> dict[str, int]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT payload_json FROM checkpoints"
            ).fetchall()
        finally:
            con.close()
        counts: dict[str, int] = {}
        for r in rows:
            try:
                s = json.loads(r["payload_json"]).get("status", "?")
            except ValueError:
                s = "?"
            counts[s] = counts.get(s, 0) + 1
        return counts
