"""Multi-backend persistence and state checkpointing for resumable graph runs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from harness.engine.schemas import RunSnapshot, TraceEvent, utc_now
from harness.events import global_event_bus


def _safe_json_dumps(obj: Any) -> str:
    """Serialize object to JSON with fallback to str for non-standard types."""
    return json.dumps(obj, sort_keys=True, default=str)


def _approval_context_digest(snapshot: RunSnapshot) -> str:
    payload = {
        "run_id": snapshot.run_id, "graph": snapshot.graph, "graph_digest": snapshot.graph_digest, "tenant_id": snapshot.tenant_id,
        "requester_id": snapshot.user_id, "roles": snapshot.roles, "input": snapshot.input,
        "state": snapshot.state, "current_node": snapshot.current_node,
        "status": snapshot.status,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class Checkpoint:
    checkpoint_id: str
    run_id: str
    label: str
    state: dict[str, Any]
    current_node: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "run_id": self.run_id,
            "label": self.label,
            "state": self.state,
            "current_node": self.current_node,
            "created_at": self.created_at,
        }


class SQLiteRunStore:
    def __init__(self, sqlite_path: str | Path) -> None:
        self.sqlite_path = Path(sqlite_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._mem_holder = self._connect() if str(self.sqlite_path) in {":memory:", ""} else None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        if str(self.sqlite_path) in {":memory:", ""}:
            conn = sqlite3.connect("file:memdb?mode=memory&cache=shared", uri=True)
        else:
            conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    graph TEXT NOT NULL,
                    graph_digest TEXT,
                    status TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    current_node TEXT,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    tenant_id TEXT NOT NULL DEFAULT 'local',
                    user_id TEXT NOT NULL DEFAULT 'local',
                    roles_json TEXT NOT NULL DEFAULT '["user"]'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trace_events (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    node TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    current_node TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                )
                """
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS approval_grants (
                    grant_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                    requester_id TEXT NOT NULL, approver_id TEXT NOT NULL, approval_type TEXT NOT NULL,
                    continuation_node TEXT NOT NULL, context_digest TEXT NOT NULL, expires_at TEXT NOT NULL,
                    consumed_at TEXT, FOREIGN KEY (run_id) REFERENCES runs(run_id)
                )"""
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
            for column, declaration in (
                ("graph_digest", "TEXT"),
                ("tenant_id", "TEXT NOT NULL DEFAULT 'local'"),
                ("user_id", "TEXT NOT NULL DEFAULT 'local'"),
                ("roles_json", "TEXT NOT NULL DEFAULT '[\"user\"]'"),
            ):
                if column not in columns:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {declaration}")
            conn.execute("PRAGMA journal_mode = WAL")

    def create_run(
        self,
        graph: str,
        input_data: dict[str, Any],
        start_node: str,
        tenant_id: str = "local",
        user_id: str = "local",
        roles: list[str] | None = None,
        graph_digest: str | None = None,
    ) -> RunSnapshot:
        snapshot = RunSnapshot(
            run_id=str(uuid4()),
            graph=graph,
            graph_digest=graph_digest,
            status="queued",
            input=input_data,
            state={},
            current_node=start_node,
            tenant_id=tenant_id,
            user_id=user_id,
            roles=roles or ["user"],
        )
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, graph, graph_digest, status, input_json, state_json, current_node,
                    result_json, error, created_at, updated_at, completed_at,
                    tenant_id, user_id, roles_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._row_values(snapshot),
            )
        global_event_bus.publish("run_created", snapshot.run_id, {"graph": graph, "start_node": start_node})
        return snapshot

    def get_run(self, run_id: str) -> RunSnapshot:
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"run not found: {run_id}")
        return self._from_row(row)

    def claim_run(self, run_id: str) -> bool:
        """Atomically transition a queued run to running across store connections."""
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "UPDATE runs SET status = 'running', updated_at = ? WHERE run_id = ? AND status = 'queued'",
                (utc_now(), run_id),
            )
            return cursor.rowcount == 1

    def issue_approval_grant(self, grant: dict[str, str]) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """INSERT INTO approval_grants
                (grant_id,run_id,tenant_id,requester_id,approver_id,approval_type,continuation_node,context_digest,expires_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                tuple(grant[k] for k in ("grant_id", "run_id", "tenant_id", "requester_id", "approver_id", "approval_type", "continuation_node", "context_digest", "expires_at")),
            )

    def consume_approval_grant(self, grant_id: str, context_digest: str) -> tuple[RunSnapshot, dict[str, str]] | None:
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM approval_grants WHERE grant_id=?", (grant_id,)).fetchone()
            if row is None or row["consumed_at"] is not None or row["expires_at"] <= utc_now() or row["context_digest"] != context_digest:
                return None
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (row["run_id"],)).fetchone()
            if run is None or run["status"] != "needs_human" or run["tenant_id"] != row["tenant_id"]:
                return None
            current_snapshot = self._from_row(run)
            if _approval_context_digest(current_snapshot) != context_digest:
                return None
            state = json.loads(run["state_json"])
            if state.get("required_approval") != row["approval_type"] or state.get("resume_node") != row["continuation_node"]:
                return None
            now = utc_now()
            conn.execute("UPDATE approval_grants SET consumed_at=? WHERE grant_id=? AND consumed_at IS NULL", (now, grant_id))
            state.pop("required_approval", None)
            state.pop("resume_node", None)
            conn.execute("UPDATE runs SET status='queued',current_node=?,state_json=?,updated_at=? WHERE run_id=? AND status='needs_human'", (row["continuation_node"], _safe_json_dumps(state), now, row["run_id"]))
            updated = conn.execute("SELECT * FROM runs WHERE run_id=?", (row["run_id"],)).fetchone()
            receipt = {"run_id": row["run_id"], "tenant_id": row["tenant_id"], "approver_id": row["approver_id"], "approval_type": row["approval_type"], "continuation_node": row["continuation_node"], "approved_at": now}
            return self._from_row(updated), receipt

    def delete_run(self, run_id: str) -> None:
        with self._lock, self._connection() as conn:
            exists = conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if exists is None:
                raise KeyError(f"run not found: {run_id}")
            conn.execute("DELETE FROM trace_events WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM checkpoints WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM approval_grants WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))

    def save_run(self, snapshot: RunSnapshot) -> RunSnapshot:
        snapshot.updated_at = utc_now()
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE runs
                   SET graph = ?,
                       graph_digest = ?,
                       status = ?,
                       input_json = ?,
                       state_json = ?,
                       current_node = ?,
                       result_json = ?,
                       error = ?,
                       created_at = ?,
                       updated_at = ?,
                       completed_at = ?,
                       tenant_id = ?,
                       user_id = ?,
                       roles_json = ?
                 WHERE run_id = ?
                """,
                (
                    snapshot.graph,
                    snapshot.graph_digest,
                    snapshot.status,
                    _safe_json_dumps(snapshot.input),
                    _safe_json_dumps(snapshot.state),
                    snapshot.current_node,
                    _safe_json_dumps(snapshot.result) if snapshot.result is not None else None,
                    snapshot.error,
                    snapshot.created_at,
                    snapshot.updated_at,
                    snapshot.completed_at,
                    snapshot.tenant_id,
                    snapshot.user_id,
                    _safe_json_dumps(snapshot.roles),
                    snapshot.run_id,
                ),
            )
        global_event_bus.publish(
            "run_updated",
            snapshot.run_id,
            {"status": snapshot.status, "current_node": snapshot.current_node},
            node=snapshot.current_node,
        )
        return snapshot

    def append_trace(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        node: str | None = None,
    ) -> TraceEvent:
        payload = payload or {}
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM trace_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            seq = int(row["next_seq"])
            event = TraceEvent(
                run_id=run_id,
                seq=seq,
                timestamp=utc_now(),
                node=node,
                event_type=event_type,
                payload=payload,
            )
            conn.execute(
                """
                INSERT INTO trace_events
                    (run_id, seq, timestamp, node, event_type, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    seq,
                    event.timestamp,
                    node,
                    event_type,
                    _safe_json_dumps(payload),
                ),
            )
        global_event_bus.publish(event_type, run_id, payload, node=node)
        return event

    def list_trace(self, run_id: str, after_seq: int = 0) -> list[TraceEvent]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trace_events WHERE run_id = ? AND seq > ? ORDER BY seq ASC",
                (run_id, after_seq),
            ).fetchall()
        return [
            TraceEvent(
                run_id=row["run_id"],
                seq=row["seq"],
                timestamp=row["timestamp"],
                node=row["node"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def create_checkpoint(self, run_id: str, label: str = "") -> Checkpoint:
        """Create an immutable state checkpoint for time-travel or rewind."""
        snapshot = self.get_run(run_id)
        chk_id = f"chk_{uuid4().hex[:8]}"
        created = utc_now()
        checkpoint = Checkpoint(
            checkpoint_id=chk_id,
            run_id=run_id,
            label=label or f"Checkpoint at {snapshot.current_node or 'end'}",
            state=dict(snapshot.state),
            current_node=snapshot.current_node,
            created_at=created,
        )
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO checkpoints (checkpoint_id, run_id, label, state_json, current_node, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    chk_id,
                    run_id,
                    checkpoint.label,
                    _safe_json_dumps(checkpoint.state),
                    checkpoint.current_node,
                    created,
                ),
            )
        self.append_trace(run_id, "checkpoint_created", {"checkpoint_id": chk_id, "label": checkpoint.label})
        return checkpoint

    def list_checkpoints(self, run_id: str) -> list[Checkpoint]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
        return [
            Checkpoint(
                checkpoint_id=r["checkpoint_id"],
                run_id=r["run_id"],
                label=r["label"],
                state=json.loads(r["state_json"]),
                current_node=r["current_node"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def rewind_to_checkpoint(self, run_id: str, checkpoint_id: str) -> RunSnapshot:
        """Time-travel rewind: roll back run state and node pointer to a checkpoint."""
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE run_id = ? AND checkpoint_id = ?",
                (run_id, checkpoint_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"checkpoint {checkpoint_id!r} not found for run {run_id!r}")

        snapshot = self.get_run(run_id)
        snapshot.state = json.loads(row["state_json"])
        snapshot.current_node = row["current_node"]
        snapshot.status = "queued"
        snapshot.error = None
        self.save_run(snapshot)
        self.append_trace(
            run_id,
            "checkpoint_rewound",
            {"checkpoint_id": checkpoint_id, "restored_node": snapshot.current_node},
        )
        return snapshot

    def _row_values(self, snapshot: RunSnapshot) -> tuple[Any, ...]:
        return (
            snapshot.run_id,
            snapshot.graph,
            snapshot.graph_digest,
            snapshot.status,
            _safe_json_dumps(snapshot.input),
            _safe_json_dumps(snapshot.state),
            snapshot.current_node,
            _safe_json_dumps(snapshot.result) if snapshot.result is not None else None,
            snapshot.error,
            snapshot.created_at,
            snapshot.updated_at,
            snapshot.completed_at,
            snapshot.tenant_id,
            snapshot.user_id,
            _safe_json_dumps(snapshot.roles),
        )

    def _from_row(self, row: sqlite3.Row) -> RunSnapshot:
        return RunSnapshot(
            run_id=row["run_id"],
            graph=row["graph"],
            graph_digest=row["graph_digest"],
            status=row["status"],
            input=json.loads(row["input_json"]),
            state=json.loads(row["state_json"]),
            current_node=row["current_node"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            roles=json.loads(row["roles_json"]),
        )


class InMemoryRunStore:
    """Ephemeral in-memory store for serverless execution, unit tests, and fast scratch runs."""

    def __init__(self) -> None:
        self._runs: dict[str, RunSnapshot] = {}
        self._traces: dict[str, list[TraceEvent]] = {}
        self._checkpoints: dict[str, list[Checkpoint]] = {}
        self._approval_grants: dict[str, dict[str, str]] = {}
        self._lock = threading.RLock()

    def create_run(
        self,
        graph: str,
        input_data: dict[str, Any],
        start_node: str,
        tenant_id: str = "local",
        user_id: str = "local",
        roles: list[str] | None = None,
        graph_digest: str | None = None,
    ) -> RunSnapshot:
        snapshot = RunSnapshot(
            run_id=str(uuid4()),
            graph=graph,
            graph_digest=graph_digest,
            status="queued",
            input=input_data,
            state={},
            current_node=start_node,
            tenant_id=tenant_id,
            user_id=user_id,
            roles=roles or ["user"],
        )
        with self._lock:
            self._runs[snapshot.run_id] = snapshot
            self._traces[snapshot.run_id] = []
            self._checkpoints[snapshot.run_id] = []
        global_event_bus.publish("run_created", snapshot.run_id, {"graph": graph, "start_node": start_node})
        return snapshot

    def get_run(self, run_id: str) -> RunSnapshot:
        with self._lock:
            if run_id not in self._runs:
                raise KeyError(f"run not found: {run_id}")
            return self._runs[run_id]

    def claim_run(self, run_id: str) -> bool:
        with self._lock:
            snapshot = self._runs.get(run_id)
            if snapshot is None or snapshot.status != "queued":
                return False
            snapshot.status = "running"
            snapshot.updated_at = utc_now()
            return True

    def issue_approval_grant(self, grant: dict[str, str]) -> None:
        with self._lock:
            self._approval_grants[grant["grant_id"]] = dict(grant)

    def consume_approval_grant(self, grant_id: str, context_digest: str) -> tuple[RunSnapshot, dict[str, str]] | None:
        with self._lock:
            grant = self._approval_grants.get(grant_id)
            if not grant or grant.get("consumed_at") or grant["expires_at"] <= utc_now() or grant["context_digest"] != context_digest:
                return None
            snapshot = self._runs.get(grant["run_id"])
            if snapshot is None or snapshot.status != "needs_human" or snapshot.tenant_id != grant["tenant_id"]:
                return None
            if _approval_context_digest(snapshot) != context_digest:
                return None
            if snapshot.state.get("required_approval") != grant["approval_type"] or snapshot.state.get("resume_node") != grant["continuation_node"]:
                return None
            now = utc_now()
            grant["consumed_at"] = now
            snapshot.state.pop("required_approval", None)
            snapshot.state.pop("resume_node", None)
            snapshot.status = "queued"
            snapshot.current_node = grant["continuation_node"]
            snapshot.updated_at = now
            return snapshot, {"run_id": snapshot.run_id, "tenant_id": snapshot.tenant_id, "approver_id": grant["approver_id"], "approval_type": grant["approval_type"], "continuation_node": grant["continuation_node"], "approved_at": now}

    def delete_run(self, run_id: str) -> None:
        with self._lock:
            if run_id not in self._runs:
                raise KeyError(f"run not found: {run_id}")
            self._runs.pop(run_id, None)
            self._traces.pop(run_id, None)
            self._checkpoints.pop(run_id, None)

    def save_run(self, snapshot: RunSnapshot) -> RunSnapshot:
        snapshot.updated_at = utc_now()
        with self._lock:
            self._runs[snapshot.run_id] = snapshot
        global_event_bus.publish(
            "run_updated",
            snapshot.run_id,
            {"status": snapshot.status, "current_node": snapshot.current_node},
            node=snapshot.current_node,
        )
        return snapshot

    def append_trace(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        node: str | None = None,
    ) -> TraceEvent:
        payload = payload or {}
        with self._lock:
            traces = self._traces.setdefault(run_id, [])
            seq = len(traces) + 1
            event = TraceEvent(
                run_id=run_id,
                seq=seq,
                timestamp=utc_now(),
                node=node,
                event_type=event_type,
                payload=payload,
            )
            traces.append(event)
        global_event_bus.publish(event_type, run_id, payload, node=node)
        return event

    def list_trace(self, run_id: str, after_seq: int = 0) -> list[TraceEvent]:
        with self._lock:
            return [event for event in self._traces.get(run_id, []) if event.seq > after_seq]

    def create_checkpoint(self, run_id: str, label: str = "") -> Checkpoint:
        snapshot = self.get_run(run_id)
        chk = Checkpoint(
            checkpoint_id=f"chk_{uuid4().hex[:8]}",
            run_id=run_id,
            label=label or f"Checkpoint at {snapshot.current_node or 'end'}",
            state=dict(snapshot.state),
            current_node=snapshot.current_node,
            created_at=utc_now(),
        )
        with self._lock:
            self._checkpoints.setdefault(run_id, []).append(chk)
        self.append_trace(run_id, "checkpoint_created", {"checkpoint_id": chk.checkpoint_id, "label": chk.label})
        return chk

    def list_checkpoints(self, run_id: str) -> list[Checkpoint]:
        with self._lock:
            return list(self._checkpoints.get(run_id, []))

    def rewind_to_checkpoint(self, run_id: str, checkpoint_id: str) -> RunSnapshot:
        with self._lock:
            chks = self._checkpoints.get(run_id, [])
            match = next((c for c in chks if c.checkpoint_id == checkpoint_id), None)
            if not match:
                raise KeyError(f"checkpoint {checkpoint_id!r} not found for run {run_id!r}")
            snapshot = self.get_run(run_id)
            snapshot.state = dict(match.state)
            snapshot.current_node = match.current_node
            snapshot.status = "queued"
            snapshot.error = None
            self.save_run(snapshot)
            self.append_trace(
                run_id,
                "checkpoint_rewound",
                {"checkpoint_id": checkpoint_id, "restored_node": snapshot.current_node},
            )
            return snapshot
