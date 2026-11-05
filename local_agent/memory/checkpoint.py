"""Checkpoint and resume — persist agent state after each step.

Checkpoints are stored as JSON files alongside the SQLite database
so a crashed or paused run can resume from the last completed step.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Checkpoint:
    """Serialisable snapshot of the agent state at a given step."""

    run_id: str
    goal: str
    step_index: int
    plan: list[str]
    scratchpad: str
    status: str
    messages_snapshot: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class CheckpointStore:
    """File-based checkpoint storage."""

    def __init__(self, directory: Path | str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in run_id)[:80]
        return self._dir / f"checkpoint_{safe}.json"

    def save(self, checkpoint: Checkpoint) -> Path:
        """Save a checkpoint, overwriting any previous one for the same run."""
        path = self._path(checkpoint.run_id)
        path.write_text(json.dumps(checkpoint.to_dict(), indent=2), encoding="utf-8")
        return path

    def load(self, run_id: str) -> Checkpoint | None:
        """Load the most recent checkpoint for a run, or None."""
        path = self._path(run_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return Checkpoint.from_dict(data)

    def delete(self, run_id: str) -> None:
        path = self._path(run_id)
        if path.exists():
            path.unlink()

    def list_checkpoints(self) -> list[str]:
        """Return run IDs that have checkpoints."""
        return [
            p.stem.removeprefix("checkpoint_")
            for p in self._dir.glob("checkpoint_*.json")
        ]
