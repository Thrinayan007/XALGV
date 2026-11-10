"""Additive SQLite workflow journal. In-flight records are never replayed here."""

import json
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from .base import AgentResult, ArtifactManifest, TaskState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY, goal TEXT NOT NULL, contract TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running', result TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS agent_tasks (
    run_id TEXT NOT NULL REFERENCES agent_runs(run_id),
    task_id TEXT NOT NULL, spec TEXT NOT NULL, status TEXT NOT NULL,
    result TEXT, PRIMARY KEY (run_id, task_id)
);
CREATE TABLE IF NOT EXISTS task_dependencies (
    run_id TEXT NOT NULL, task_id TEXT NOT NULL, producer_task_id TEXT NOT NULL,
    PRIMARY KEY (run_id, task_id, producer_task_id),
    FOREIGN KEY (run_id, task_id) REFERENCES agent_tasks(run_id, task_id),
    FOREIGN KEY (run_id, producer_task_id) REFERENCES agent_tasks(run_id, task_id)
);
CREATE TABLE IF NOT EXISTS task_attempts (
    run_id TEXT NOT NULL, task_id TEXT NOT NULL, attempt INTEGER NOT NULL,
    status TEXT NOT NULL, result TEXT, started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT, PRIMARY KEY (run_id, task_id, attempt),
    FOREIGN KEY (run_id, task_id) REFERENCES agent_tasks(run_id, task_id)
);
CREATE TABLE IF NOT EXISTS task_artifacts (
    run_id TEXT NOT NULL, task_id TEXT NOT NULL, path TEXT NOT NULL, manifest TEXT NOT NULL,
    PRIMARY KEY (run_id, task_id, path),
    FOREIGN KEY (run_id, task_id) REFERENCES agent_tasks(run_id, task_id)
);
CREATE TABLE IF NOT EXISTS task_verifications (
    run_id TEXT NOT NULL, task_id TEXT NOT NULL, attempt INTEGER NOT NULL, report TEXT NOT NULL,
    PRIMARY KEY (run_id, task_id, attempt),
    FOREIGN KEY (run_id, task_id, attempt) REFERENCES task_attempts(run_id, task_id, attempt)
);
CREATE TABLE IF NOT EXISTS agent_events (
    sequence INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES agent_runs(run_id),
    task_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS resource_leases (
    lease_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
    browser INTEGER NOT NULL, acquired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, closed_at TEXT,
    FOREIGN KEY (run_id, task_id) REFERENCES agent_tasks(run_id, task_id)
);
CREATE INDEX IF NOT EXISTS agent_events_run ON agent_events(run_id, sequence);
"""


class WorkflowStore:
    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        if read_only:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                version = 1
            if version == 1:
                db.execute("ALTER TABLE agent_runs ADD COLUMN deadline REAL")
                db.execute("ALTER TABLE agent_runs ADD COLUMN token_charge INTEGER NOT NULL DEFAULT 0")
                db.execute("ALTER TABLE agent_tasks ADD COLUMN token_charge INTEGER NOT NULL DEFAULT 0")
                db.execute("PRAGMA user_version=2")
            elif version != 2:
                raise ValueError(f"Unsupported workflow journal version: {version}")

    @contextmanager
    def _connection(self):
        location = self.path.resolve().as_uri() + "?mode=ro" if self.read_only else self.path
        with closing(sqlite3.connect(location, uri=self.read_only)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            yield db

    @staticmethod
    def _event(db, run_id, task_id, kind, payload):
        db.execute("INSERT INTO agent_events(run_id,task_id,kind,payload) VALUES (?,?,?,?)",
                   (run_id, task_id, kind, json.dumps(payload, separators=(",", ":"))))

    def event(self, run_id, task_id, kind, payload):
        with self._connection() as db:
            self._event(db, run_id, task_id, kind, payload)

    def open_resources(self, task, *, browser: bool):
        lease_id = uuid4().hex
        with self._connection() as db:
            db.execute("INSERT INTO resource_leases(lease_id,run_id,task_id,browser) VALUES (?,?,?,?)",
                       (lease_id, task.run_id, task.task_id, browser))
            self._event(db, task.run_id, task.task_id, "resources_acquired", {"lease_id": lease_id, "browser": browser})
        return lease_id

    def close_resources(self, task, lease_id):
        with self._connection() as db:
            db.execute("UPDATE resource_leases SET closed_at=CURRENT_TIMESTAMP WHERE lease_id=? AND run_id=? AND task_id=?",
                       (lease_id, task.run_id, task.task_id))
            self._event(db, task.run_id, task.task_id, "resource_scope_closed", {"lease_id": lease_id})

    def start(self, goal, graph, *, deadline=None, token_charge=0):
        run_id = graph.request.run_id
        with self._connection() as db:
            db.execute("INSERT INTO agent_runs(run_id,goal,contract,deadline,token_charge) VALUES (?,?,?,?,?)",
                       (run_id, goal[:4000], graph.request.model_dump_json(),
                        deadline if deadline is not None else time.time() + graph.request.max_wall_time_seconds, token_charge))
            db.executemany("INSERT INTO agent_tasks(run_id,task_id,spec,status) VALUES (?,?,?,?)",
                           [(run_id, key, graph.task(key).model_dump_json(), TaskState.PENDING) for key in graph.order])
            self._record_edges(db, graph, graph.order)
            self._event(db, run_id, None, "graph_admitted", {"order": graph.order})

    def account(self, run_id, delta, task_id=None):
        with self._connection() as db:
            db.execute("UPDATE agent_runs SET token_charge=token_charge+? WHERE run_id=?", (delta, run_id))
            if task_id is not None:
                db.execute("UPDATE agent_tasks SET token_charge=token_charge+? WHERE run_id=? AND task_id=?",
                           (delta, run_id, task_id))

    def attempt_count(self, run_id, task_id):
        with self._connection() as db:
            return db.execute("SELECT COALESCE(MAX(attempt),0) FROM task_attempts WHERE run_id=? AND task_id=?",
                              (run_id, task_id)).fetchone()[0]

    def attempt_results(self, run_id, task_id):
        with self._connection() as db:
            rows = db.execute("SELECT result FROM task_attempts WHERE run_id=? AND task_id=? "
                              "AND result IS NOT NULL ORDER BY attempt", (run_id, task_id))
            return [AgentResult.model_validate_json(row[0]) for row in rows]

    def task_token_charge(self, run_id, task_id):
        with self._connection() as db:
            return db.execute("SELECT token_charge FROM agent_tasks WHERE run_id=? AND task_id=?",
                              (run_id, task_id)).fetchone()[0]

    def resume(self, run_id):
        """Fence abandoned attempts. External outcomes are left for reconciliation."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE agent_runs SET status='running' WHERE run_id=?", (run_id,))
            active = db.execute("SELECT task_id,status FROM agent_tasks WHERE run_id=? AND status IN ('running','verifying')",
                                (run_id,)).fetchall()
            for task_id, _ in active:
                self._transition(db, run_id, task_id, TaskState.OUTCOME_UNKNOWN)
            db.execute("UPDATE task_attempts SET status='outcome_unknown',ended_at=CURRENT_TIMESTAMP "
                       "WHERE run_id=? AND status='running'", (run_id,))
            db.execute("UPDATE resource_leases SET closed_at=CURRENT_TIMESTAMP WHERE run_id=? AND closed_at IS NULL",
                       (run_id,))
            self._event(db, run_id, None, "resume", {"abandoned_tasks": [item[0] for item in active]})

    @staticmethod
    def _record_edges(db, graph, task_ids):
        run_id = graph.request.run_id
        for key in task_ids:
            db.execute("DELETE FROM task_dependencies WHERE run_id=? AND task_id=?", (run_id, key))
            db.executemany("INSERT INTO task_dependencies VALUES (?,?,?)",
                           [(run_id, key, parent) for parent in graph.task(key).dependencies])

    def revise(self, graph, finished):
        run_id = graph.request.run_id
        pending = [key for key in graph.order if key not in finished]
        with self._connection() as db:
            for key in pending:
                changed = db.execute("UPDATE agent_tasks SET spec=? WHERE run_id=? AND task_id=? AND status=?",
                                     (graph.task(key).model_dump_json(), run_id, key, TaskState.PENDING))
                if changed.rowcount != 1:
                    raise RuntimeError("Cannot revise a started task")
            self._record_edges(db, graph, pending)
            self._event(db, run_id, None, "graph_revised", {"order": graph.order})

    def _transition(self, db, run_id, task_id, state):
        old = TaskState(db.execute("SELECT status FROM agent_tasks WHERE run_id=? AND task_id=?",
                                   (run_id, task_id)).fetchone()[0])
        if old == state:
            return
        if not old.allows(state):
            raise RuntimeError(f"Invalid task transition: {old} → {state}")
        updated = db.execute("UPDATE agent_tasks SET status=? WHERE run_id=? AND task_id=? AND status=?",
                             (state, run_id, task_id, old))
        if updated.rowcount != 1:
            raise RuntimeError("Task state changed concurrently")
        self._event(db, run_id, task_id, "transition", {"from": old, "to": state})

    def transition(self, run_id, task_id, state):
        with self._connection() as db:
            self._transition(db, run_id, task_id, state)

    def begin_attempt(self, task, attempt):
        with self._connection() as db:
            self._transition(db, task.run_id, task.task_id, TaskState.RUNNING)
            db.execute("INSERT INTO task_attempts(run_id,task_id,attempt,status) VALUES (?,?,?,?)",
                       (task.run_id, task.task_id, attempt, TaskState.RUNNING))

    def end_attempt(self, task, attempt, outcome, result=None):
        with self._connection() as db:
            changed = db.execute(
                "UPDATE task_attempts SET status=?,result=?,ended_at=CURRENT_TIMESTAMP "
                "WHERE run_id=? AND task_id=? AND attempt=? AND status=?",
                (outcome, result.model_dump_json() if result else None, task.run_id, task.task_id, attempt, TaskState.RUNNING),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Attempt was not running")

    def finish_task(self, run_id, execution, *, reconciled=False):
        task_id = execution.task_id
        with self._connection() as db:
            if reconciled:
                self._transition(db, run_id, task_id, TaskState.VERIFYING)
                self._event(db, run_id, task_id, "operator_reconciled", {"attempt": execution.verification.attempt})
            self._transition(db, run_id, task_id, execution.state)
            if report := execution.verification:
                db.execute("INSERT INTO task_verifications VALUES (?,?,?,?)",
                           (run_id, task_id, report.attempt, report.model_dump_json()))
            if execution.state == TaskState.COMPLETED:
                db.executemany("INSERT INTO task_artifacts VALUES (?,?,?,?)", [
                    (run_id, task_id, item.path, item.model_dump_json()) for item in execution.attempts[-1].artifacts
                ])
            db.execute("UPDATE agent_tasks SET result=? WHERE run_id=? AND task_id=?",
                       (execution.model_dump_json(), run_id, task_id))

    def finish(self, result):
        with self._connection() as db:
            db.execute("UPDATE agent_runs SET status=?,result=? WHERE run_id=?",
                       ("completed" if result.completed else "incomplete", result.model_dump_json(), result.run_id))

    def manifests(self, run_id, producers):
        """Read only committed artifacts for verified producers in this run."""
        indexed = {}
        with self._connection() as db:
            for producer in producers:
                rows = db.execute(
                    "SELECT a.path,a.manifest FROM task_artifacts a JOIN agent_tasks t USING(run_id,task_id) "
                    "WHERE a.run_id=? AND a.task_id=? AND t.status=?",
                    (run_id, producer, TaskState.COMPLETED),
                )
                indexed[producer] = {path: ArtifactManifest.model_validate_json(manifest) for path, manifest in rows}
        return indexed

    def interrupt(self, run_id):
        with self._connection() as db:
            db.execute("UPDATE agent_runs SET status='interrupted' WHERE run_id=? AND status='running'", (run_id,))
            self._event(db, run_id, None, "interrupted", {})

    def inspect(self, run_id):
        """Trusted inspection only; all reads are scoped to one run ID."""
        with self._connection() as db:
            db.execute("BEGIN")
            db.row_factory = sqlite3.Row
            run = db.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("Unknown workflow run")
            result = {"run": dict(run)}
            for table in ("agent_tasks", "task_dependencies", "task_attempts", "task_artifacts",
                          "task_verifications", "agent_events", "resource_leases"):
                rows = db.execute(f"SELECT * FROM {table} WHERE run_id=? ORDER BY rowid", (run_id,))
                result[table] = [dict(row) for row in rows]
            return result
