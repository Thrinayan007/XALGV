"""Ollama model manager with role-based VRAM-aware scheduling.

The manager maps logical roles (planner, vision, embed, …) to concrete
Ollama models and ensures only one large model is resident at a time within
the VRAM budget.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from local_agent.config import AgentConfig, ModelRoleConfig

logger = logging.getLogger(__name__)


# ── Response types ───────────────────────────────────────────────────────

@dataclass
class ToolCall:
    """A single tool invocation returned by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResponse:
    """Parsed response from an Ollama /api/chat call."""

    content: str
    tool_calls: list[ToolCall]
    model: str
    role: str
    done: bool
    total_duration_ns: int = 0
    eval_count: int = 0
    prompt_eval_count: int = 0

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


@dataclass
class EmbedResponse:
    """Parsed response from an Ollama /api/embed call."""

    embeddings: list[list[float]]
    model: str


# ── Model Manager ────────────────────────────────────────────────────────

class ModelManager:
    """Manages Ollama model loading and inference, one model at a time.

    This is the single interface the agent core uses for all LLM work.
    It handles:
    * Role → model mapping via :class:`AgentConfig`.
    * Model loading / unloading to stay within the VRAM budget.
    * Chat completions (with optional tool calling).
    * Embedding requests.
    """

    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.ollama_base_url,
            timeout=httpx.Timeout(connect=15, read=360, write=60, pool=10),
        )
        self._current_model: str | None = None
        self._lock = asyncio.Lock()

    # ── Public API ───────────────────────────────────────────────────────

    async def chat(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        """Send a chat completion request using the model for *role*."""
        role_cfg = self._role_config(role)
        await self._ensure_model_loaded(role_cfg.model)

        payload: dict[str, Any] = {
            "model": role_cfg.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature if temperature is not None else role_cfg.temperature,
                "num_ctx": role_cfg.context_length,
            },
        }
        if max_tokens is not None:
            payload["options"]["num_predict"] = max_tokens
        if tools:
            payload["tools"] = tools

        resp = await self._client.post("/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()

        # Parse tool calls if present.
        tool_calls: list[ToolCall] = []
        msg = data.get("message", {})
        for i, tc in enumerate(msg.get("tool_calls", [])):
            fn = tc.get("function", {})
            tool_calls.append(ToolCall(
                id=f"call_{i}",
                name=fn.get("name", ""),
                arguments=fn.get("arguments", {}),
            ))

        return ChatResponse(
            content=msg.get("content", ""),
            tool_calls=tool_calls,
            model=data.get("model", role_cfg.model),
            role=msg.get("role", "assistant"),
            done=data.get("done", True),
            total_duration_ns=data.get("total_duration", 0),
            eval_count=data.get("eval_count", 0),
            prompt_eval_count=data.get("prompt_eval_count", 0),
        )

    async def embed(self, texts: list[str], *, role: str = "embed") -> EmbedResponse:
        """Compute embeddings for a list of texts."""
        role_cfg = self._role_config(role)
        await self._ensure_model_loaded(role_cfg.model)

        resp = await self._client.post("/api/embed", json={
            "model": role_cfg.model,
            "input": texts,
        })
        resp.raise_for_status()
        data = resp.json()

        return EmbedResponse(
            embeddings=data.get("embeddings", []),
            model=data.get("model", role_cfg.model),
        )

    async def list_running(self) -> list[dict[str, Any]]:
        """List models currently loaded in Ollama."""
        resp = await self._client.get("/api/ps")
        resp.raise_for_status()
        return resp.json().get("models", [])

    async def health_check(self) -> bool:
        """Return True if Ollama is reachable."""
        try:
            resp = await self._client.get("/api/tags")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        await self._client.aclose()

    # ── Internal ─────────────────────────────────────────────────────────

    def _role_config(self, role: str) -> ModelRoleConfig:
        cfg = self._config.model_roles.get(role)
        if cfg is None:
            raise ValueError(f"No model configured for role '{role}'. "
                             f"Available roles: {list(self._config.model_roles)}")
        return cfg

    async def _ensure_model_loaded(self, model: str) -> None:
        """Load *model* if it is not already the current resident model.

        Ollama handles loading/unloading internally, but we track which model
        we last used so we can log swaps and avoid unnecessary keep-alive
        requests.
        """
        async with self._lock:
            if self._current_model == model:
                return

            if self._current_model is not None:
                logger.info("Model swap: %s → %s", self._current_model, model)
                # Ask Ollama to unload the previous model by setting keep_alive to 0.
                try:
                    await self._client.post("/api/generate", json={
                        "model": self._current_model,
                        "keep_alive": 0,
                    })
                except httpx.HTTPError:
                    logger.warning("Failed to unload model %s", self._current_model)

            # Pre-load the new model with a keep-alive.
            logger.info("Loading model: %s", model)
            try:
                await self._client.post("/api/generate", json={
                    "model": model,
                    "keep_alive": "10m",
                })
            except httpx.HTTPError:
                logger.warning("Failed to pre-load model %s — will try on first call", model)

            self._current_model = model
