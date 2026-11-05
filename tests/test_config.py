"""Tests for the configuration system."""

import os
import unittest
from pathlib import Path

from local_agent.config import AgentConfig, ModelRoleConfig


class TestAgentConfig(unittest.TestCase):
    def test_default_config(self):
        cfg = AgentConfig()
        self.assertEqual(cfg.ollama_base_url, "http://localhost:11434")
        self.assertEqual(cfg.sandbox_url, "http://localhost:9000")
        self.assertEqual(cfg.max_steps, 50)
        self.assertIn("planner", cfg.model_roles)

    def test_ensure_dirs_creates_directories(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AgentConfig(
                workspace_dir=Path(tmp) / "ws",
                db_path=Path(tmp) / "data" / "test.db",
                trace_dir=Path(tmp) / "traces",
            )
            cfg.ensure_dirs()
            self.assertTrue(cfg.workspace_dir.exists())
            self.assertTrue(cfg.db_path.parent.exists())
            self.assertTrue(cfg.trace_dir.exists())

    def test_from_env(self):
        os.environ["XALGV_MAX_STEPS"] = "99"
        try:
            cfg = AgentConfig.from_env()
            self.assertEqual(cfg.max_steps, 99)
        finally:
            del os.environ["XALGV_MAX_STEPS"]

    def test_model_role_config(self):
        role = ModelRoleConfig(model="qwen3:8b", context_length=8192, temperature=0.6, thinking=True)
        self.assertEqual(role.model, "qwen3:8b")
        self.assertTrue(role.thinking)


if __name__ == "__main__":
    unittest.main()
