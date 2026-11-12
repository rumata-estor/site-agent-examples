"""Contract checks for original Task Manager with local adapters for private Runtime dependencies."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from tempfile import TemporaryDirectory
import unittest

from tests._runtime_stubs import install_environment_stubs

# Every test module installs adapters before importing the public code.
install_environment_stubs()
from siteagent.tasks import TaskStore, TaskError


def _process_create(manager_root: str, gate, output, index: int):
    """Independent processes, each with its own flock file descriptor."""
    store = TaskStore("demo", {
        "paths": {"manager_root": manager_root},
        "task_manager": {"enabled": True},
    })
    gate.wait(timeout=10)
    try:
        output.put(("created", store.create(f"Process {index}")["id"]))
    except TaskError as exc:
        output.put(("rejected", str(exc)))


class TaskManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="site-agent-example-tasks-")
        self.cfg = {"paths": {"manager_root": self.tmp.name}, "task_manager": {"enabled": True}}
        self.store = TaskStore("demo", self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lifecycle_and_invalid_transitions(self):
        created = self.store.create("Update a page")
        task_id = created["id"]
        self.assertEqual(self.store.get()["status"], "running")
        with self.assertRaisesRegex(TaskError, "Invalid task transition"):
            self.store.transition(task_id, "running")
        self.store.transition(task_id, "paused", reason="User requested pause")
        self.assertEqual(self.store.get(task_id)["status"], "paused")
        self.store.transition(task_id, "running")
        self.store.transition(task_id, "verifying")
        self.store.transition(task_id, "done", reason="Checked")
        self.assertEqual(self.store.get(task_id)["result"], "Checked")
        with self.assertRaises(TaskError):
            self.store.transition(task_id, "running")
        with self.assertRaises(TaskError):
            self.store.checkpoint(task_id, done="too late")

    def test_missing_objective_rejected(self):
        with self.assertRaisesRegex(TaskError, "objective is required"):
            self.store.create("   ")

    def test_checkpoint_is_persisted_with_revision(self):
        task_id = self.store.create("Fix a header")["id"]
        update = self.store.checkpoint(task_id, note="Test", done="Inspected layout", next_step="Fix CSS")
        self.assertEqual(update["revision"], 2)
        reopened = TaskStore("demo", self.cfg)
        self.assertEqual(reopened.get(task_id)["completed_steps"][-1]["text"], "Inspected layout")
        self.assertEqual(reopened.get(task_id)["next_step"], "Fix CSS")

    def test_block_requires_reason(self):
        task_id = self.store.create("Fix template")["id"]
        with self.assertRaises(TaskError):
            self.store.transition(task_id, "blocked")
        self.store.transition(task_id, "blocked", reason="Needs review")
        self.assertEqual(self.store.get(task_id)["blocker"], "Needs review")

    def test_busy_task_blocks_second_create(self):
        first = self.store.create("First")["id"]
        with self.assertRaisesRegex(TaskError, "Active task"):
            self.store.create("Second")
        self.assertEqual(self.store.active_id(), first)

    def test_incomplete_create_recovers_missing_pointer(self):
        first = self.store.create("First")["id"]
        self.store.active_path.unlink()  # simulate crash before active.json publication
        reopened = TaskStore("demo", self.cfg)
        self.assertEqual(reopened.active_id(), first)
        self.assertEqual(reopened.get("active")["id"], first)
        with self.assertRaises(TaskError):
            reopened.create("Conflicting")

    def test_interrupted_transition_recovers_stale_pointer(self):
        first = self.store.create("First")["id"]
        self.store.transition(first, "paused")
        second = self.store.create("Second")["id"]
        self.store._set_active_locked(first)  # model crash after B became running
        self.assertEqual(TaskStore("demo", self.cfg).active_id(), second)
        with self.assertRaises(TaskError):
            self.store.create("Should not be allowed")

    def test_nonbusy_transition_cannot_hide_other_running_task(self):
        first = self.store.create("First")["id"]
        self.store.transition(first, "paused")
        second = self.store.create("Second")["id"]
        self.store.transition(first, "cancelled", reason="Not needed")
        self.assertEqual(self.store.active_id(), second)
        with self.assertRaises(TaskError):
            self.store.create("Should be blocked")

    def test_older_paused_task_cannot_resume_while_another_is_busy(self):
        first = self.store.create("First")["id"]
        self.store.transition(first, "paused")
        second = self.store.create("Second")["id"]
        with self.assertRaises(TaskError):
            self.store.transition(first, "running")
        self.store.transition(second, "done")
        self.store.transition(first, "running")
        self.assertEqual(self.store.active_id(), first)

    def test_terminal_pointer_recovered_after_interrupted_cleanup(self):
        task_id = self.store.create("Finish")["id"]
        self.store.transition(task_id, "done")
        self.store._set_active_locked(task_id)  # simulate crash before pointer removal
        self.assertIsNone(TaskStore("demo", self.cfg).active_id())
        self.assertEqual(self.store.create("New")["status"], "running")

    def test_missing_pointer_with_paused_task_has_no_active_task(self):
        task_id = self.store.create("First")["id"]
        self.store.transition(task_id, "paused")
        self.store.active_path.unlink()
        self.assertIsNone(TaskStore("demo", self.cfg).active_id())
        self.assertEqual(self.store.get(task_id)["status"], "paused")

    def test_concurrent_task_creation_has_exactly_one_success(self):
        def attempt(i):
            try:
                return self.store.create(f"Task {i}")["id"]
            except TaskError:
                return None
        with ThreadPoolExecutor(max_workers=8) as executor:
            created = list(executor.map(attempt, range(16)))
        self.assertEqual(sum(x is not None for x in created), 1)
        self.assertEqual(self.store.active_id(), next(x for x in created if x))

    def test_independent_processes_cannot_create_two_busy_tasks(self):
        ctx = get_context("fork")  # fcntl is a POSIX-only dependency
        gate = ctx.Barrier(4)
        results = ctx.Queue()
        processes = [
            ctx.Process(target=_process_create, args=(self.tmp.name, gate, results, i))
            for i in range(4)
        ]
        for process in processes:
            process.start()
        try:
            outcomes = [results.get(timeout=10) for _ in processes]
        finally:
            for process in processes:
                process.join(timeout=10)
                if process.is_alive():
                    process.terminate()
                    process.join()
        self.assertTrue(all(process.exitcode == 0 for process in processes))
        self.assertEqual([result[0] for result in outcomes].count("created"), 1)
        self.assertEqual(len(self.store.list(status="running")), 1)

    def test_invalid_persisted_task_fails_closed(self):
        created = self.store.create("First")
        path = self.store._task_path(created["id"])
        path.write_text("not valid json", encoding="utf-8")
        with self.assertRaisesRegex(TaskError, "Cannot inspect task"):
            self.store.create("Should be blocked")

    def test_inconsistent_busy_tasks_fail_closed(self):
        first = self.store.create("First")["id"]
        second_id = first[:-4] + ("abcd" if not first.endswith("abcd") else "dcba")
        document = self.store.get(first)
        document["id"] = second_id
        self.store._atomic_json(self.store._task_path(second_id), document)
        with self.assertRaisesRegex(TaskError, "busy tasks"):
            self.store.active_id()

    def test_list_and_status_filters(self):
        first = self.store.create("First")["id"]
        self.store.transition(first, "done")
        second = self.store.create("Second")["id"]
        self.assertEqual([x["id"] for x in self.store.list("running")], [second])
        self.assertEqual(len(self.store.list()), 2)


if __name__ == "__main__":
    unittest.main()
