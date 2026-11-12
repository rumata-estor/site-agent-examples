from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
from pathlib import Path
import json
import os
import secrets

from .permissions import ensure_shared_directory, apply_shared_file_mode, chmod_if_owned

from .tasks import TASK_ID_RE


class StateGuardError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_hash(data) -> str:
    payload = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _aware_time(value: str, field: str) -> datetime:
    """Parse an ISO 8601 timestamp and require an explicit UTC offset."""
    if not isinstance(value, str) or not value.strip():
        raise StateGuardError(f"Invalid {field}: expected timezone-aware timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone offset is missing")
    except (TypeError, ValueError) as exc:
        raise StateGuardError(f"Invalid {field}: {value!r}") from exc
    return parsed.astimezone(timezone.utc)


class StaleStateStore:
    """Durable invalidation marker shared by Resolver/DecisionSet/ChangeSet."""

    def __init__(self, site_id: str, cfg: dict):
        self.site_id = site_id
        manager_root = (cfg.get("paths") or {}).get("manager_root")
        if not manager_root:
            raise StateGuardError(
                f"paths.manager_root is not configured for site {site_id}"
            )
        self.root = Path(manager_root) / "stale-state"
        ensure_shared_directory(self.root)
        self.lock_path = self.root / ".lock"

    @contextmanager
    def _locked(self):
        """Serialize read/increment/replace across threads and processes."""
        self.lock_path.touch(mode=0o660, exist_ok=True)
        with self.lock_path.open("r+") as handle:
            apply_shared_file_mode(self.lock_path)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _path(self, task_id: str) -> Path:
        if not TASK_ID_RE.fullmatch(str(task_id or "")):
            raise StateGuardError(f"Invalid task id: {task_id!r}")
        return self.root / f"{task_id}.json"

    @staticmethod
    def _atomic_json(path: Path, data: dict) -> None:
        tmp = path.with_name(
            f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        )
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        with tmp.open("x", encoding="utf-8") as fh:
            apply_shared_file_mode(tmp)
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        apply_shared_file_mode(path)

    def mark(self, task_id: str, diagnostic: dict) -> dict:
        """Record a stale observation without losing concurrent revisions.

        A corrupted prior marker is *not* treated as missing: resetting its
        revision would hide a consistency error that requires inspection.
        """
        path = self._path(task_id)
        with self._locked():
            previous = self.get(task_id)
            previous_revision = int((previous or {}).get("revision") or 0)
            if previous_revision < 0:
                raise StateGuardError(f"Invalid stale-state revision for {task_id}")
            document = {
                "format": 1,
                "site_id": self.site_id,
                "task_id": task_id,
                "status": "STALE_STATE",
                "at": utc_now(),
                "revision": previous_revision + 1,
                "diagnostic": dict(diagnostic or {}),
            }
            self._atomic_json(path, document)
            return document

    def get(self, task_id: str) -> dict | None:
        path = self._path(task_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise StateGuardError(
                f"Invalid stale-state marker for {task_id}: {exc}"
            ) from exc
        if (
            not isinstance(data, dict)
            or data.get("site_id") != self.site_id
            or data.get("task_id") != task_id
            or data.get("status") != "STALE_STATE"
        ):
            raise StateGuardError(f"Stale-state marker identity mismatch: {task_id}")
        if (
            type(data.get("format")) is not int
            or data["format"] != 1
            or type(data.get("revision")) is not int
            or data["revision"] < 1
            or not isinstance(data.get("diagnostic"), dict)
        ):
            raise StateGuardError(f"Invalid stale-state marker fields: {task_id}")
        _aware_time(data.get("at"), "stale-state marker timestamp")
        return data

    def invalidates(self, task_id: str, document_time: str | None) -> bool:
        marker = self.get(task_id)
        if not marker:
            return False
        if document_time is None or document_time == "":
            return True
        # Timestamp strings with different offsets do not sort chronologically.
        return _aware_time(marker["at"], "stale-state marker timestamp") > _aware_time(
            document_time, "document timestamp"
        )
