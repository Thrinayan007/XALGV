"""Persistent memory store backed by SQLite.

Stores:
* **Runs** — goal, status, timestamps, and summary for each agent run.
* **Steps** — every observation → decision → action → result within a run.
* **Skills** — verified procedures saved for reuse (procedural memory).

The schema is append-only by default to preserve a full audit trail.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class RunStatus(str, Enum):
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    AWAITING_HUMAN = "awaiting_human"
    KILLED = "killed"


@dataclass
class StepRecord:
    """A single step in an agent run."""

    step_index: int
    action_type: str  # e.g. "tool_call", "plan", "observe", "verify"
    tool_name: str | None = None
    tool_params: dict[str, Any] | None = None
    observation: str = ""
    decision: str = ""
    result_output: str = ""
    result_success: bool | None = None
    verification_status: str = "skipped"
    verification_message: str = ""
    error: str | None = None
    timestamp: str = field(default_factory=_utc_now)


@dataclass
class RunRecord:
    """Summary of a complete agent run."""

    run_id: str
    goal: str
    status: RunStatus
    steps: list[StepRecord] = field(default_factory=list)
    plan: list[str] = field(default_factory=list)
    summary: str = ""
    created_at: str = field(default_factory=_utc_now)
    completed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillRecord:
    """A reusable procedure extracted from a successful run."""

    skill_id: str
    name: str
    description: str
    procedure: list[dict[str, Any]]
    source_run_id: str
    created_at: str = field(default_factory=_utc_now)
    success_count: int = 0
    tags: list[str] = field(default_factory=list)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    goal         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'planning',
    plan         TEXT NOT NULL DEFAULT '[]',
    summary      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    completed_at TEXT,
    metadata     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS steps (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id               TEXT NOT NULL REFERENCES runs(run_id),
    step_index           INTEGER NOT NULL,
    action_type          TEXT NOT NULL,
    tool_name            TEXT,
    tool_params          TEXT,
    observation          TEXT NOT NULL DEFAULT '',
    decision             TEXT NOT NULL DEFAULT '',
    result_output        TEXT NOT NULL DEFAULT '',
    result_success       INTEGER,
    verification_status  TEXT NOT NULL DEFAULT 'skipped',
    verification_message TEXT NOT NULL DEFAULT '',
    error                TEXT,
    timestamp            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id, step_index);

CREATE TABLE IF NOT EXISTS skills (
    skill_id      TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    description   TEXT NOT NULL DEFAULT '',
    procedure     TEXT NOT NULL DEFAULT '[]',
    source_run_id TEXT,
    created_at    TEXT NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 0,
    tags          TEXT NOT NULL DEFAULT '[]'
);
"""


class MemoryStore:
    """SQLite-backed persistent memory."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── Connection management ────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self._db_path), isolation_level="DEFERRED")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Runs ─────────────────────────────────────────────────────────────

    def create_run(self, run_id: str, goal: str) -> RunRecord:
        record = RunRecord(run_id=run_id, goal=goal, status=RunStatus.PLANNING)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, goal, status, plan, summary, created_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record.run_id, record.goal, record.status.value,
                 json.dumps(record.plan), record.summary, record.created_at,
                 json.dumps(record.metadata)),
            )
        return record

    def update_run_status(self, run_id: str, status: RunStatus, *, summary: str | None = None) -> None:
        with self._connect() as conn:
            if summary is not None:
                conn.execute(
                    "UPDATE runs SET status=?, summary=?, completed_at=? WHERE run_id=?",
                    (status.value, summary, _utc_now() if status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.KILLED) else None, run_id),
                )
            else:
                conn.execute("UPDATE runs SET status=? WHERE run_id=?", (status.value, run_id))

    def update_run_plan(self, run_id: str, plan: list[str]) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE runs SET plan=? WHERE run_id=?", (json.dumps(plan), run_id))

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None
            steps = self._load_steps(conn, run_id)
            return RunRecord(
                run_id=row["run_id"], goal=row["goal"],
                status=RunStatus(row["status"]),
                plan=json.loads(row["plan"]),
                summary=row["summary"],
                steps=steps,
                created_at=row["created_at"],
                completed_at=row["completed_at"],
                metadata=json.loads(row["metadata"]),
            )

    def list_runs(self, limit: int = 20) -> list[RunRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [
                RunRecord(
                    run_id=r["run_id"], goal=r["goal"],
                    status=RunStatus(r["status"]),
                    plan=json.loads(r["plan"]),
                    summary=r["summary"], created_at=r["created_at"],
                    completed_at=r["completed_at"],
                    metadata=json.loads(r["metadata"]),
                )
                for r in rows
            ]

    # ── Steps ────────────────────────────────────────────────────────────

    def record_step(self, run_id: str, step: StepRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO steps "
                "(run_id, step_index, action_type, tool_name, tool_params, "
                "observation, decision, result_output, result_success, "
                "verification_status, verification_message, error, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, step.step_index, step.action_type, step.tool_name,
                 json.dumps(step.tool_params) if step.tool_params else None,
                 step.observation, step.decision, step.result_output,
                 int(step.result_success) if step.result_success is not None else None,
                 step.verification_status, step.verification_message,
                 step.error, step.timestamp),
            )

    def _load_steps(self, conn: sqlite3.Connection, run_id: str) -> list[StepRecord]:
        rows = conn.execute(
            "SELECT * FROM steps WHERE run_id=? ORDER BY step_index", (run_id,)
        ).fetchall()
        return [
            StepRecord(
                step_index=r["step_index"],
                action_type=r["action_type"],
                tool_name=r["tool_name"],
                tool_params=json.loads(r["tool_params"]) if r["tool_params"] else None,
                observation=r["observation"],
                decision=r["decision"],
                result_output=r["result_output"],
                result_success=bool(r["result_success"]) if r["result_success"] is not None else None,
                verification_status=r["verification_status"],
                verification_message=r["verification_message"],
                error=r["error"],
                timestamp=r["timestamp"],
            )
            for r in rows
        ]

    # ── Skills (procedural memory) ───────────────────────────────────────

    def save_skill(self, skill: SkillRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO skills "
                "(skill_id, name, description, procedure, source_run_id, "
                "created_at, success_count, tags) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (skill.skill_id, skill.name, skill.description,
                 json.dumps(skill.procedure), skill.source_run_id,
                 skill.created_at, skill.success_count,
                 json.dumps(skill.tags)),
            )

    def find_skill(self, name: str) -> SkillRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM skills WHERE name=?", (name,)).fetchone()
            if row is None:
                return None
            return SkillRecord(
                skill_id=row["skill_id"], name=row["name"],
                description=row["description"],
                procedure=json.loads(row["procedure"]),
                source_run_id=row["source_run_id"],
                created_at=row["created_at"],
                success_count=row["success_count"],
                tags=json.loads(row["tags"]),
            )

    def list_skills(self) -> list[SkillRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM skills ORDER BY name").fetchall()
            return [
                SkillRecord(
                    skill_id=r["skill_id"], name=r["name"],
                    description=r["description"],
                    procedure=json.loads(r["procedure"]),
                    source_run_id=r["source_run_id"],
                    created_at=r["created_at"],
                    success_count=r["success_count"],
                    tags=json.loads(r["tags"]),
                )
                for r in rows
            ]
