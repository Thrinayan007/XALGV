"""Centralised configuration for the local agent."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class ModelRoleConfig:
    """Maps a logical role to a concrete Ollama model tag."""

    model: str
    context_length: int = 8192
    temperature: float = 0.6
    thinking: bool = False  # Use /think mode for this role


# Tool-use roles share one model/context to avoid role-switch reloads.
# DEFAULT_MODEL_ROLES: dict[str, ModelRoleConfig] = {
#     "planner": ModelRoleConfig(model="qwen3:8b", context_length=8192, temperature=0.6, thinking=True),
#     "executor": ModelRoleConfig(model="qwen3:8b", context_length=8192, temperature=0.3),
#     "router": ModelRoleConfig(model="qwen3:1.7b", context_length=4096, temperature=0.1),
#     "vision": ModelRoleConfig(model="qwen2.5-vl:7b", context_length=4096, temperature=0.3),
#     "embed": ModelRoleConfig(model="bge-m3", context_length=8192, temperature=0.0),
#     "translate": ModelRoleConfig(model="qwen3:8b", context_length=8192, temperature=0.3),
# }
DEFAULT_MODEL_ROLES: dict[str, ModelRoleConfig] = {
    "planner": ModelRoleConfig(
        model="qwen3.5:9b",
        context_length=8192,
        temperature=0.0,
        thinking=False
    ),

    "executor": ModelRoleConfig(
        model="qwen3.5:9b",
        # context_length=4096,
        # temperature=0.3
        context_length=8192,
        temperature=0.0,
        thinking=False
    ),

    "router": ModelRoleConfig(
        model="qwen3.5:9b",
        context_length=4096,
        temperature=0.1
    ),

    "vision": ModelRoleConfig(
        model="qwen3.5:9b",
        context_length=8192,
        temperature=0.0
    ),

    "embed": ModelRoleConfig(
        model="bge-m3",
        context_length=8192,
        temperature=0.0
    ),

    "translate": ModelRoleConfig(
        model="qwen3.5:9b",
        context_length=8192,
        temperature=0.3
    ),
}

@dataclass
class AgentConfig:
    """Top-level agent configuration.  All paths default to sensible locations
    relative to the project root, and every value can be overridden through
    environment variables prefixed with ``XALGV_``."""

    # ── Ollama ───────────────────────────────────────────────────────────
    ollama_base_url: str = "http://localhost:11434"
    vram_budget_mb: int = 8192
    model_roles: dict[str, ModelRoleConfig] = field(default_factory=lambda: dict(DEFAULT_MODEL_ROLES))

    # ── Sandbox ──────────────────────────────────────────────────────────
    sandbox_url: str = "http://localhost:9000"
    sandbox_container: str = "xalgv_sandbox"

    # ── Budgets & limits ─────────────────────────────────────────────────
    max_steps: int = 50
    max_wall_time_seconds: int = 600
    max_retries_per_step: int = 3
    max_repeated_actions: int = 3

    # ── Paths ────────────────────────────────────────────────────────────
    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    workspace_dir: Path = field(default=None)  # type: ignore[assignment]
    data_dir: Path = field(default=None)  # type: ignore[assignment]
    db_path: Path = field(default=None)  # type: ignore[assignment]
    trace_dir: Path = field(default=None)  # type: ignore[assignment]

    # ── Search ───────────────────────────────────────────────────────────
    searxng_base_url: str = "http://localhost:8080"

    def __post_init__(self) -> None:
        if self.workspace_dir is None:
            self.workspace_dir = self.project_root / "workspace"
        if self.data_dir is None:
            self.data_dir = self.project_root / "data"
        if self.db_path is None:
            self.db_path = self.project_root / "data" / "agent.db"
        if self.trace_dir is None:
            self.trace_dir = self.project_root / "traces"

    # ── Helpers ──────────────────────────────────────────────────────────

    def ensure_dirs(self) -> None:
        """Create all required directories if they do not exist."""
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.trace_dir.mkdir(parents=True, exist_ok=True)

    def use_generation_model(self, model_tag: str) -> None:
        """Override text/image generation roles without changing the embedder."""
        for role, role_config in self.model_roles.items():
            if role != "embed":
                self.model_roles[role] = replace(role_config, model=model_tag)

    @classmethod
    def from_env(cls) -> AgentConfig:
        """Build a config using ``XALGV_*`` environment variables as overrides."""
        overrides: dict[str, object] = {}
        env_map = {
            "XALGV_OLLAMA_URL": "ollama_base_url",
            "XALGV_SANDBOX_URL": "sandbox_url",
            "XALGV_VRAM_MB": ("vram_budget_mb", int),
            "XALGV_MAX_STEPS": ("max_steps", int),
            "XALGV_MAX_TIME": ("max_wall_time_seconds", int),
            "XALGV_SEARXNG_URL": "searxng_base_url",
            "XALGV_DB_PATH": ("db_path", Path),
            "XALGV_WORKSPACE": ("workspace_dir", Path),
            "XALGV_TRACE_DIR": ("trace_dir", Path),
        }
        for env_key, spec in env_map.items():
            value = os.environ.get(env_key)
            if value is None:
                continue
            if isinstance(spec, str):
                overrides[spec] = value
            else:
                attr_name, converter = spec
                overrides[attr_name] = converter(value)
        return cls(**overrides)
