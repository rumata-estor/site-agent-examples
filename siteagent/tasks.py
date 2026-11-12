from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import fcntl
import json
import os
import re
import secrets

from .permissions import ensure_shared_directory, apply_shared_file_mode, chmod_if_owned

from .config import load_site


TASK_ID_RE = re.compile(r"^TASK-\d{8}-\d{6}-[0-9a-f]{4}$")
TERMINAL_STATUSES = {"done", "failed", "cancelled"}
BUSY_STATUSES = {"running", "verifying"}
VALID_STATUSES = BUSY_STATUSES | {"paused", "blocked"} | TERMINAL_STATUSES

TRANSITIONS = {
    "running": {"paused", "blocked", "verifying", "done", "failed", "cancelled"},
    "paused": {"running", "failed", "cancelled"},
    "blocked": {"running", "failed", "cancelled"},
    "verifying": {"running", "blocked", "done", "failed", "cancelled"},
}


class TaskError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(
    name: str,
    value: str | None,
    limit: int,
    required: bool = False,
) -> str | None:
    if value is None:
        if required:
            raise TaskError(f"{name} is required")
        return None
    text = str(value).strip()
    if required and not text:
        raise TaskError(f"{name} is required")
    if len(text) > limit:
        raise TaskError(f"{name} is too long (max {limit})")
    return text or None


class TaskStore:
    def __init__(self, site_id: str, cfg: dict):
        self.site_id = site_id
        task_cfg = cfg.get("task_manager") or {}
        if not task_cfg.get("enabled", False):
            raise TaskError(f"Task Manager is disabled for site {site_id}")

        manager_root = (cfg.get("paths") or {}).get("manager_root")
        if not manager_root:
            raise TaskError(f"paths.manager_root is not configured for site {site_id}")

        self.root = Path(manager_root) / "tasks"
        ensure_shared_directory(self.root)

        self.lock_path = self.root / ".lock"
        self.active_path = self.root / "active.json"
        self.checkpoint_keep = max(1, int(task_cfg.get("checkpoint_keep", 20)))
        self.completed_keep = max(1, int(task_cfg.get("completed_keep", 50)))

    @classmethod
    def from_site(cls, site_id: str) -> "TaskStore":
        return cls(site_id, load_site(site_id))

    @contextmanager
    def _locked(self):
        self.lock_path.touch(mode=0o660, exist_ok=True)
        with self.lock_path.open("r+") as fh:
            apply_shared_file_mode(self.lock_path)
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _atomic_json(self, path: Path, data: dict) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        with tmp.open("x", encoding="utf-8") as fh:
            apply_shared_file_mode(tmp)
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        apply_shared_file_mode(path)

    @staticmethod
    def _read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def _task_path(self, task_id: str) -> Path:
        if not TASK_ID_RE.fullmatch(task_id):
            raise TaskError(f"Invalid task id: {task_id}")
        return self.root / f"{task_id}.json"

    def _new_task_id(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        for _ in range(20):
            task_id = f"TASK-{stamp}-{secrets.token_hex(2)}"
            if not self._task_path(task_id).exists():
                return task_id
        raise TaskError("Could not allocate task id")

    def _busy_tasks_locked(self) -> list[dict]:
        """Find busy tasks independently of the recoverable active pointer.

        A task record and active.json are separate atomic files. A process can
        crash after publishing a new task state but before updating the pointer.
        Never use that pointer as the sole concurrency guard.
        """
        busy = []
        for path in self.root.glob("TASK-*.json"):
            if not TASK_ID_RE.fullmatch(path.stem):
                raise TaskError(f"Invalid task filename: {path.name}")
            try:
                task = self._read_json(path)
            except (OSError, ValueError, TypeError) as exc:
                raise TaskError(f"Cannot inspect task {path.name}: {exc}") from exc
            if not isinstance(task, dict):
                raise TaskError(f"Invalid task record: {path.name}")
            if task.get("site_id") != self.site_id or task.get("id") != path.stem:
                raise TaskError(f"Task file identity mismatch: {path.name}")
            if task.get("status") not in VALID_STATUSES:
                raise TaskError(f"Invalid task status: {path.name}")
            if task["status"] in BUSY_STATUSES:
                busy.append(task)
        return busy

    def _active_id_locked(self) -> str | None:
        pointer = None
        if self.active_path.is_file():
            try:
                data = self._read_json(self.active_path)
                pointer = data.get("task_id")
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                raise TaskError(f"Invalid active task pointer: {exc}") from exc
            if not isinstance(pointer, str) or not TASK_ID_RE.fullmatch(pointer):
                raise TaskError("Invalid active task pointer id")

        busy = self._busy_tasks_locked()
        if len(busy) > 1:
            raise TaskError("Inconsistent state: multiple busy tasks; manual recovery required")
        if busy:
            task_id = busy[0]["id"]
            if pointer != task_id:
                self._set_active_locked(task_id)
            return task_id

        # Preserve a deliberately paused/blocked current task, but clear a
        # pointer left behind by an interrupted completion or cancellation.
        if pointer:
            path = self._task_path(pointer)
            if path.is_file():
                task = self._read_json(path)
                if task.get("site_id") == self.site_id and task.get("id") == pointer:
                    if task.get("status") in {"paused", "blocked"}:
                        return pointer
            self._set_active_locked(None)
        return None

    def _set_active_locked(self, task_id: str | None) -> None:
        if task_id is None:
            self.active_path.unlink(missing_ok=True)
            return
        self._atomic_json(self.active_path, {"task_id": task_id, "updated_at": utc_now()})

    def _load_task_locked(self, task_ref: str) -> dict:
        task_id = task_ref
        if task_ref == "active":
            task_id = self._active_id_locked() or ""
            if not task_id:
                raise TaskError(f"No active task for site {self.site_id}")
        path = self._task_path(task_id)
        if not path.is_file():
            raise TaskError(f"Unknown task: {task_id}")
        task = self._read_json(path)
        if task.get("site_id") != self.site_id or task.get("id") != task_id:
            raise TaskError(f"Task file identity mismatch: {task_id}")
        return task

    def _active_conflict_locked(self, target_id: str) -> dict | None:
        active_id = self._active_id_locked()
        if not active_id or active_id == target_id:
            return None
        active_path = self._task_path(active_id)
        if not active_path.is_file():
            self._set_active_locked(None)
            return None
        active = self._read_json(active_path)
        if active.get("status") in BUSY_STATUSES:
            return active
        return None

    def active_id(self) -> str | None:
        with self._locked():
            return self._active_id_locked()

    def _append_checkpoint(
        self,
        task: dict,
        kind: str,
        note: str | None = None,
        done: str | None = None,
    ) -> None:
        item = {
            "at": utc_now(),
            "kind": kind,
            "status": task["status"],
            "current_step": task.get("current_step"),
            "next_step": task.get("next_step"),
        }
        if note:
            item["note"] = note
        if done:
            item["done"] = done
        checkpoints = list(task.get("checkpoints") or [])
        checkpoints.append(item)
        task["checkpoints"] = checkpoints[-self.checkpoint_keep :]

        if done:
            completed = list(task.get("completed_steps") or [])
            completed.append({"at": item["at"], "text": done})
            task["completed_steps"] = completed[-self.completed_keep :]

    def create(
        self,
        objective: str,
        source: str | None = None,
        current_step: str | None = None,
        next_step: str | None = None,
    ) -> dict:
        objective = clean_text("objective", objective, 4000, required=True)
        source = clean_text("source", source, 200)
        current_step = clean_text("current_step", current_step, 1000)
        next_step = clean_text("next_step", next_step, 1000)

        with self._locked():
            task_id = self._new_task_id()
            conflict = self._active_conflict_locked(task_id)
            if conflict:
                raise TaskError(
                    f"Active task {conflict['id']} is {conflict['status']}; "
                    "pause or block it before creating another task"
                )
            now = utc_now()
            task = {
                "format": 1,
                "id": task_id,
                "site_id": self.site_id,
                "objective": objective,
                "status": "running",
                "source": source,
                "current_step": current_step,
                "next_step": next_step,
                "blocker": None,
                "result": None,
                "failure_reason": None,
                "cancel_reason": None,
                "completed_steps": [],
                "checkpoints": [],
                "created_at": now,
                "updated_at": now,
                "revision": 1,
            }
            self._append_checkpoint(task, "created", note=source)
            self._atomic_json(self._task_path(task_id), task)
            self._set_active_locked(task_id)
            return task

    def get(self, task_ref: str = "active") -> dict:
        with self._locked():
            return self._load_task_locked(task_ref)

    def list(self, status: str | None = None, limit: int = 50) -> list[dict]:
        if status is not None and status not in VALID_STATUSES:
            raise TaskError(f"Invalid status: {status}")
        limit = max(1, min(int(limit), 500))
        with self._locked():
            active_id = self._active_id_locked()
            tasks = []
            for path in sorted(self.root.glob("TASK-*.json"), reverse=True):
                try:
                    task = self._read_json(path)
                except Exception:
                    continue
                if task.get("site_id") != self.site_id:
                    continue
                if status is not None and task.get("status") != status:
                    continue
                tasks.append({
                    "id": task.get("id"),
                    "objective": task.get("objective"),
                    "status": task.get("status"),
                    "current_step": task.get("current_step"),
                    "next_step": task.get("next_step"),
                    "blocker": task.get("blocker"),
                    "updated_at": task.get("updated_at"),
                    "active": task.get("id") == active_id,
                })
                if len(tasks) >= limit:
                    break
            return tasks

    def checkpoint(
        self,
        task_ref: str = "active",
        note: str | None = None,
        done: str | None = None,
        current_step: str | None = None,
        next_step: str | None = None,
    ) -> dict:
        note = clean_text("note", note, 2000)
        done = clean_text("done", done, 2000)
        current_step = clean_text("current_step", current_step, 1000)
        next_step = clean_text("next_step", next_step, 1000)

        with self._locked():
            task = self._load_task_locked(task_ref)
            if task["status"] in TERMINAL_STATUSES:
                raise TaskError(f"Task {task['id']} is already {task['status']}")
            if current_step is not None:
                task["current_step"] = current_step
            if next_step is not None:
                task["next_step"] = next_step
            self._append_checkpoint(task, "checkpoint", note=note, done=done)
            task["updated_at"] = utc_now()
            task["revision"] = int(task.get("revision") or 0) + 1
            self._atomic_json(self._task_path(task["id"]), task)
            return task

    def transition(
        self,
        task_ref: str,
        new_status: str,
        reason: str | None = None,
        current_step: str | None = None,
        next_step: str | None = None,
    ) -> dict:
        if new_status not in VALID_STATUSES:
            raise TaskError(f"Invalid status: {new_status}")
        reason = clean_text("reason", reason, 3000)
        current_step = clean_text("current_step", current_step, 1000)
        next_step = clean_text("next_step", next_step, 1000)
        if new_status == "blocked" and not reason:
            raise TaskError("reason is required when blocking a task")

        with self._locked():
            task = self._load_task_locked(task_ref)
            old_status = str(task.get("status") or "")
            allowed = TRANSITIONS.get(old_status, set())
            if new_status not in allowed:
                raise TaskError(
                    f"Invalid task transition: {old_status} -> {new_status}"
                )

            if new_status in BUSY_STATUSES:
                conflict = self._active_conflict_locked(task["id"])
                if conflict:
                    raise TaskError(
                        f"Cannot activate {task['id']}; "
                        f"{conflict['id']} is {conflict['status']}"
                    )

            if current_step is not None:
                task["current_step"] = current_step
            if next_step is not None:
                task["next_step"] = next_step

            task["status"] = new_status
            if new_status in {"running", "verifying", "paused"}:
                task["blocker"] = None
            elif new_status == "blocked":
                task["blocker"] = reason
            elif new_status == "done":
                task["result"] = reason
                task["blocker"] = None
            elif new_status == "failed":
                task["failure_reason"] = reason
            elif new_status == "cancelled":
                task["cancel_reason"] = reason
                task["blocker"] = None

            self._append_checkpoint(
                task,
                f"status:{old_status}->{new_status}",
                note=reason,
            )
            task["updated_at"] = utc_now()
            task["revision"] = int(task.get("revision") or 0) + 1
            self._atomic_json(self._task_path(task["id"]), task)

            active_id = self._active_id_locked()
            if new_status in TERMINAL_STATUSES:
                if active_id == task["id"]:
                    self._set_active_locked(None)
            else:
                self._set_active_locked(task["id"])
            return task
