"""Tests for the memory subsystem."""

import json
import tempfile
import unittest
from pathlib import Path

from local_agent.memory.checkpoint import Checkpoint, CheckpointStore
from local_agent.memory.store import MemoryStore, RunStatus, SkillRecord, StepRecord
from local_agent.memory.working import WorkingMemory


class TestMemoryStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.store = MemoryStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_get_run(self):
        run = self.store.create_run("run_001", "Test goal")
        self.assertEqual(run.run_id, "run_001")
        self.assertEqual(run.goal, "Test goal")
        self.assertEqual(run.status, RunStatus.PLANNING)

        loaded = self.store.get_run("run_001")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.goal, "Test goal")

    def test_update_run_status(self):
        self.store.create_run("run_002", "Goal 2")
        self.store.update_run_status("run_002", RunStatus.COMPLETED, summary="Done!")
        run = self.store.get_run("run_002")
        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertEqual(run.summary, "Done!")
        self.assertIsNotNone(run.completed_at)

    def test_record_and_load_steps(self):
        self.store.create_run("run_003", "Goal 3")
        step = StepRecord(
            step_index=0,
            action_type="tool_call",
            tool_name="shell_exec",
            tool_params={"command": "ls"},
            decision="List files",
            result_output="file1.txt\nfile2.txt",
            result_success=True,
        )
        self.store.record_step("run_003", step)
        run = self.store.get_run("run_003")
        self.assertEqual(len(run.steps), 1)
        self.assertEqual(run.steps[0].tool_name, "shell_exec")
        self.assertTrue(run.steps[0].result_success)

    def test_list_runs(self):
        self.store.create_run("run_a", "Goal A")
        self.store.create_run("run_b", "Goal B")
        runs = self.store.list_runs()
        self.assertEqual(len(runs), 2)

    def test_update_plan(self):
        self.store.create_run("run_plan", "Plan goal")
        self.store.update_run_plan("run_plan", ["step 1", "step 2"])
        run = self.store.get_run("run_plan")
        self.assertEqual(run.plan, ["step 1", "step 2"])

    def test_skills(self):
        skill = SkillRecord(
            skill_id="skill_001",
            name="hello_world",
            description="Create a hello world program",
            procedure=[{"tool": "write_file", "params": {"path": "hello.py", "content": "print('hi')"}}],
            source_run_id="run_001",
            tags=["python", "hello"],
        )
        self.store.save_skill(skill)
        found = self.store.find_skill("hello_world")
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "hello_world")
        self.assertEqual(len(found.procedure), 1)

        all_skills = self.store.list_skills()
        self.assertEqual(len(all_skills), 1)

    def test_nonexistent_run(self):
        self.assertIsNone(self.store.get_run("doesnotexist"))


class TestWorkingMemory(unittest.TestCase):
    def test_add_and_get_messages(self):
        wm = WorkingMemory(max_tokens=8192, system_prompt="You are a test agent.")
        wm.add_message("user", "Hello")
        wm.add_message("assistant", "Hi there!")
        msgs = wm.get_messages()
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1]["role"], "user")
        self.assertEqual(msgs[2]["role"], "assistant")

    def test_plan_in_system_prompt(self):
        wm = WorkingMemory(system_prompt="Base prompt.")
        wm.set_plan(["Step 1", "Step 2"])
        msgs = wm.get_messages()
        self.assertIn("Step 1", msgs[0]["content"])
        self.assertIn("Step 2", msgs[0]["content"])

    def test_compaction(self):
        wm = WorkingMemory(max_tokens=200, system_prompt="Short.")
        for i in range(20):
            wm.add_message("user", f"Message {i} " + "x" * 50)
        # Should not crash and should stay within a reasonable message count.
        msgs = wm.get_messages()
        self.assertGreater(len(msgs), 0)
        # Should have dropped some messages to stay within budget.
        self.assertLess(len(msgs), 22)  # 20 user + system + possible summary

    def test_truncation(self):
        result = WorkingMemory._truncate("x" * 100, max_chars=20)
        self.assertIn("truncated", result)


class TestCheckpointStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CheckpointStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_and_load(self):
        cp = Checkpoint(
            run_id="run_cp1",
            goal="Test goal",
            step_index=3,
            plan=["a", "b", "c"],
            scratchpad="notes",
            status="executing",
        )
        self.store.save(cp)
        loaded = self.store.load("run_cp1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.run_id, "run_cp1")
        self.assertEqual(loaded.step_index, 3)
        self.assertEqual(loaded.plan, ["a", "b", "c"])

    def test_load_nonexistent(self):
        self.assertIsNone(self.store.load("nope"))

    def test_delete(self):
        cp = Checkpoint(run_id="run_del", goal="g", step_index=0, plan=[], scratchpad="", status="planning")
        self.store.save(cp)
        self.store.delete("run_del")
        self.assertIsNone(self.store.load("run_del"))

    def test_list_checkpoints(self):
        for i in range(3):
            cp = Checkpoint(run_id=f"run_{i}", goal="g", step_index=0, plan=[], scratchpad="", status="planning")
            self.store.save(cp)
        ids = self.store.list_checkpoints()
        self.assertEqual(len(ids), 3)


if __name__ == "__main__":
    unittest.main()
