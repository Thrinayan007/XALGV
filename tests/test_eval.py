"""Tests for the eval harness task loading."""

import json
import tempfile
import unittest
from pathlib import Path

from eval.harness import EvalHarness, TaskDefinition


class TestEvalHarness(unittest.TestCase):
    def test_load_shell_tasks(self):
        harness = EvalHarness()
        tasks = harness.load_tasks(category="shell")
        self.assertGreater(len(tasks), 0)
        for task in tasks:
            self.assertEqual(task.category, "shell")
            self.assertIsInstance(task.goal, str)
            self.assertGreater(len(task.goal), 0)

    def test_load_file_tasks(self):
        harness = EvalHarness()
        tasks = harness.load_tasks(category="file")
        self.assertGreater(len(tasks), 0)

    def test_load_python_tasks(self):
        harness = EvalHarness()
        tasks = harness.load_tasks(category="python")
        self.assertGreater(len(tasks), 0)

    def test_load_all_tasks(self):
        harness = EvalHarness()
        tasks = harness.load_tasks()
        self.assertGreater(len(tasks), 10)

    def test_task_definition_structure(self):
        harness = EvalHarness()
        tasks = harness.load_tasks()
        for task in tasks:
            self.assertIsInstance(task.task_id, str)
            self.assertIsInstance(task.goal, str)
            self.assertIsInstance(task.checkers, list)
            self.assertIsInstance(task.max_steps, int)

    def test_load_from_custom_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_file = Path(tmp) / "test_tasks.json"
            task_file.write_text(json.dumps({
                "tasks": [{
                    "task_id": "custom_001",
                    "goal": "Custom task",
                    "category": "custom",
                    "checkers": [],
                }]
            }))
            harness = EvalHarness(tmp)
            tasks = harness.load_tasks()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].task_id, "custom_001")


if __name__ == "__main__":
    unittest.main()
