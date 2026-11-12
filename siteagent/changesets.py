from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import fcntl
import json
import os
import re
import secrets

from .permissions import ensure_shared_directory, apply_shared_file_mode, chmod_if_owned

from .config import load_site
from .decisions import DecisionError, DecisionStore
from .resolver import Resolver, ResolverError
from .state_guard import StaleStateStore
from .tasks import TASK_ID_RE, TaskStore, TERMINAL_STATUSES


class ChangeSetError(RuntimeError):
    pass


STATES = {"draft", "sealed", "approved"}
ACTIONS = {"create", "update", "delete", "move", "configure"}
TARGET_KINDS = {
    "resource",
    "template",
    "chunk",
    "snippet",
    "plugin",
    "tv",
    "category",
    "media_source",
    "context",
}
RISK_LEVELS = {"low", "medium", "high", "critical"}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
BACKUP_STRATEGIES = {
    "none",
    "element_snapshot",
    "file_backup",
    "db_backup",
    "full_backup",
    "custom",
}
ROLLBACK_STRATEGIES = {
    "none",
    "restore_snapshot",
    "restore_file",
    "restore_db",
    "restore_full_backup",
    "compensating_actions",
    "custom",
}

READ_SET_ROLES = {"target", "dependency"}
READ_SET_TYPES = TARGET_KINDS | {"file", "system_setting"}

SENSITIVE_KEY = re.compile(
    r"(pass(word)?|token|secret|api[_-]?key|authorization|private[_-]?key)",
    re.I,
)


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


def clean_text(
    name: str,
    value,
    limit: int,
    *,
    required: bool = False,
) -> str | None:
    if value is None:
        if required:
            raise ChangeSetError(f"{name} is required")
        return None
    text = str(value).strip()
    if required and not text:
        raise ChangeSetError(f"{name} is required")
    if len(text) > limit:
        raise ChangeSetError(f"{name} is too long (max {limit})")
    return text or None


def stable_id(name: str, value) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", text):
        raise ChangeSetError(f"Invalid {name}: {text!r}")
    return text


def string_list(
    name: str,
    value,
    *,
    limit: int = 50,
    item_limit: int = 1500,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ChangeSetError(f"{name} must be a list")
    if len(value) > limit:
        raise ChangeSetError(f"{name} has too many items (max {limit})")
    return [
        clean_text(
            f"{name}[{index}]",
            item,
            item_limit,
            required=True,
        )
        for index, item in enumerate(value)
    ]


def reject_sensitive_fields(name: str, value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if SENSITIVE_KEY.search(str(key)):
                raise ChangeSetError(
                    f"{name} contains sensitive field {key!r}; "
                    "store only a protected reference or fingerprint"
                )
            reject_sensitive_fields(f"{name}.{key}", item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_sensitive_fields(f"{name}[{index}]", item)


def json_object(
    name: str,
    value,
    *,
    max_bytes: int = 64 * 1024,
    allow_none: bool = True,
):
    if value is None and allow_none:
        return None
    if not isinstance(value, dict):
        raise ChangeSetError(f"{name} must be an object")
    reject_sensitive_fields(name, value)
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    except Exception as exc:
        raise ChangeSetError(f"{name} is not JSON serializable: {exc}") from exc
    if len(raw) > max_bytes:
        raise ChangeSetError(f"{name} is too large (max {max_bytes} bytes)")
    return value


def validate_risk(raw, name: str = "risk") -> dict:
    if not isinstance(raw, dict):
        raise ChangeSetError(f"{name} must be an object")
    unknown = sorted(set(raw) - {"level", "reasons"})
    if unknown:
        raise ChangeSetError(
            f"Unknown {name} fields: " + ", ".join(unknown)
        )
    level = str(raw.get("level") or "").strip().lower()
    if level not in RISK_LEVELS:
        raise ChangeSetError(f"Invalid {name}.level: {level}")
    return {
        "level": level,
        "reasons": string_list(
            f"{name}.reasons",
            raw.get("reasons"),
            limit=20,
            item_limit=1500,
        ),
    }


def validate_mutation(raw: dict, index: int) -> dict:
    if not isinstance(raw, dict):
        raise ChangeSetError(f"mutations[{index}] must be an object")
    allowed = {
        "id",
        "target_index",
        "action",
        "kind",
        "before",
        "after",
        "fields",
        "dependencies",
        "rationale",
        "risk",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ChangeSetError(
            f"Unknown mutations[{index}] fields: " + ", ".join(unknown)
        )

    mutation_id = stable_id(
        f"mutations[{index}].id",
        raw.get("id"),
    )
    try:
        target_index = int(raw.get("target_index"))
    except Exception as exc:
        raise ChangeSetError(
            f"mutations[{index}].target_index must be an integer"
        ) from exc
    if target_index < 0:
        raise ChangeSetError(
            f"mutations[{index}].target_index must be non-negative"
        )

    action = str(raw.get("action") or "").strip().lower()
    if action not in ACTIONS:
        raise ChangeSetError(
            f"Invalid mutations[{index}].action: {action}"
        )

    kind = str(raw.get("kind") or "").strip()
    if kind not in TARGET_KINDS:
        raise ChangeSetError(
            f"Invalid mutations[{index}].kind: {kind}"
        )

    before = json_object(
        f"mutations[{index}].before",
        raw.get("before"),
    )
    after = json_object(
        f"mutations[{index}].after",
        raw.get("after"),
    )
    if action == "create":
        if before not in (None, {}, {"exists": False}):
            raise ChangeSetError(
                f"mutations[{index}].before for create must be empty "
                "or {'exists': false}"
            )
        if not after:
            raise ChangeSetError(
                f"mutations[{index}].after is required for create"
            )
    elif action == "delete":
        if not before:
            raise ChangeSetError(
                f"mutations[{index}].before is required for delete"
            )
        if after not in (None, {}, {"exists": False}):
            raise ChangeSetError(
                f"mutations[{index}].after for delete must be empty "
                "or {'exists': false}"
            )
    else:
        if not before:
            raise ChangeSetError(
                f"mutations[{index}].before is required for {action}"
            )
        if not after:
            raise ChangeSetError(
                f"mutations[{index}].after is required for {action}"
            )

    return {
        "id": mutation_id,
        "target_index": target_index,
        "action": action,
        "kind": kind,
        "before": before,
        "after": after,
        "fields": string_list(
            f"mutations[{index}].fields",
            raw.get("fields"),
            limit=50,
            item_limit=200,
        ),
        "dependencies": string_list(
            f"mutations[{index}].dependencies",
            raw.get("dependencies"),
            limit=50,
            item_limit=1000,
        ),
        "rationale": clean_text(
            f"mutations[{index}].rationale",
            raw.get("rationale"),
            3000,
            required=True,
        ),
        "risk": validate_risk(
            raw.get("risk") or {},
            f"mutations[{index}].risk",
        ),
    }


def validate_backup(raw) -> dict:
    if not isinstance(raw, dict):
        raise ChangeSetError("backup must be an object")
    allowed = {"required", "strategy", "scope", "reason"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ChangeSetError(
            "Unknown backup fields: " + ", ".join(unknown)
        )
    required = raw.get("required", True)
    if not isinstance(required, bool):
        raise ChangeSetError("backup.required must be boolean")
    strategy = str(raw.get("strategy") or "").strip()
    if strategy not in BACKUP_STRATEGIES:
        raise ChangeSetError(
            f"Invalid backup.strategy: {strategy}"
        )
    if required and strategy == "none":
        raise ChangeSetError(
            "backup.strategy cannot be none when backup.required=true"
        )
    return {
        "required": required,
        "strategy": strategy,
        "scope": string_list(
            "backup.scope",
            raw.get("scope"),
            limit=50,
            item_limit=1000,
        ),
        "reason": clean_text(
            "backup.reason",
            raw.get("reason"),
            2000,
            required=True,
        ),
    }


BROWSER_ASSERTION_TYPES = {
    "exists", "absent", "count", "text_equals", "text_contains",
    "attribute_equals", "style_equals", "visible",
}

def validate_browser_assertions(raw, name: str) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 30:
        raise ChangeSetError(f"{name} must be a list with at most 30 assertions")
    result = []
    for index, item in enumerate(raw):
        label = f"{name}[{index}]"
        if not isinstance(item, dict) or set(item) - {
            "type", "selector", "expected", "attribute", "property", "min", "max"
        }:
            raise ChangeSetError(f"{label} has unsupported fields")
        kind = item.get("type")
        if kind not in BROWSER_ASSERTION_TYPES:
            raise ChangeSetError(f"{label}.type is unsupported")
        selector = clean_text(f"{label}.selector", item.get("selector"), 300, required=True)
        if not selector or "\\0" in selector:
            raise ChangeSetError(f"{label}.selector is unsafe")
        normalized = {"type": kind, "selector": selector}
        if kind == "count":
            minimum, maximum = item.get("min"), item.get("max")
            if minimum is None and maximum is None:
                raise ChangeSetError(f"{label} needs min or max")
            for key, value in (("min", minimum), ("max", maximum)):
                if value is not None:
                    if type(value) is not int or not 0 <= value <= 100000:
                        raise ChangeSetError(f"{label}.{key} must be a nonnegative integer")
                    normalized[key] = value
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ChangeSetError(f"{label}.min exceeds max")
        if kind in {"text_equals", "text_contains", "attribute_equals", "style_equals"}:
            expected = item.get("expected")
            if not isinstance(expected, str) or len(expected) > 2000:
                raise ChangeSetError(f"{label}.expected must be a string")
            normalized["expected"] = expected
        for field, applicable in (("attribute", "attribute_equals"), ("property", "style_equals")):
            if kind == applicable:
                value = item.get(field)
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_:-]{0,79}", value):
                    raise ChangeSetError(f"{label}.{field} is invalid")
                normalized[field] = value
        allowed = {"type", "selector"}
        if kind == "count": allowed |= {"min", "max"}
        if kind in {"text_equals", "text_contains", "attribute_equals", "style_equals"}: allowed.add("expected")
        if kind == "attribute_equals": allowed.add("attribute")
        if kind == "style_equals": allowed.add("property")
        if set(item) - allowed:
            raise ChangeSetError(f"{label} contains fields not used by {kind}")
        result.append(normalized)
    return result


def validate_verification(raw) -> list[dict]:
    if not isinstance(raw, list):
        raise ChangeSetError("verification must be a list")
    if not raw:
        raise ChangeSetError("verification must contain at least one check")
    if len(raw) > 50:
        raise ChangeSetError("verification has too many checks (max 50)")
    result = []
    ids = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ChangeSetError(
                f"verification[{index}] must be an object"
            )
        allowed = {
            "id",
            "type",
            "target",
            "expectation",
            "required",
            "assertions",
            "width",
        }
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise ChangeSetError(
                f"Unknown verification[{index}] fields: "
                + ", ".join(unknown)
            )
        check_id = stable_id(
            f"verification[{index}].id",
            item.get("id"),
        )
        if check_id in ids:
            raise ChangeSetError(
                f"Duplicate verification id: {check_id}"
            )
        ids.add(check_id)
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise ChangeSetError(
                f"verification[{index}].required must be boolean"
            )
        check_type = str(item.get("type") or "").lower()
        browser_type = check_type in {"browser", "browser_mobile", "browser_check", "browser_check_mobile"}
        if not browser_type and ("assertions" in item or "width" in item):
            raise ChangeSetError("Only browser checks support assertions and width")
        assertions = validate_browser_assertions(item.get("assertions"), f"verification[{index}].assertions")
        width = item.get("width")
        if width is not None and (type(width) is not int or not 240 <= width <= 5000):
            raise ChangeSetError(f"verification[{index}].width must be 240..5000")
        result.append({
            "assertions": assertions,
            "width": width,
            "id": check_id,
            "type": clean_text(
                f"verification[{index}].type",
                item.get("type"),
                100,
                required=True,
            ),
            "target": clean_text(
                f"verification[{index}].target",
                item.get("target"),
                1000,
                required=True,
            ),
            "expectation": clean_text(
                f"verification[{index}].expectation",
                item.get("expectation"),
                2000,
                required=True,
            ),
            "required": required,
        })
    return result


def validate_rollback(raw) -> dict:
    if not isinstance(raw, dict):
        raise ChangeSetError("rollback must be an object")
    allowed = {
        "strategy",
        "steps",
        "automatic_on_failure",
        "reason",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ChangeSetError(
            "Unknown rollback fields: " + ", ".join(unknown)
        )
    strategy = str(raw.get("strategy") or "").strip()
    if strategy not in ROLLBACK_STRATEGIES:
        raise ChangeSetError(
            f"Invalid rollback.strategy: {strategy}"
        )
    automatic = raw.get("automatic_on_failure", False)
    if not isinstance(automatic, bool):
        raise ChangeSetError(
            "rollback.automatic_on_failure must be boolean"
        )
    steps = string_list(
        "rollback.steps",
        raw.get("steps"),
        limit=50,
        item_limit=1500,
    )
    if strategy != "none" and not steps:
        raise ChangeSetError(
            "rollback.steps are required unless strategy=none"
        )
    if strategy == "none" and automatic:
        raise ChangeSetError(
            "rollback.automatic_on_failure cannot be true when strategy=none"
        )
    return {
        "strategy": strategy,
        "steps": steps,
        "automatic_on_failure": automatic,
        "reason": clean_text(
            "rollback.reason",
            raw.get("reason"),
            2000,
            required=True,
        ),
    }



def validate_read_set(raw) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ChangeSetError("read_set must be a list")
    if len(raw) > 200:
        raise ChangeSetError("read_set has too many items (max 200)")

    result = []
    seen = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ChangeSetError(
                f"read_set[{index}] must be an object"
            )
        allowed = {
            "role",
            "type",
            "kind",
            "id",
            "name",
            "key",
            "path",
            "fingerprint",
            "fields",
        }
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise ChangeSetError(
                f"Unknown read_set[{index}] fields: "
                + ", ".join(unknown)
            )

        role = str(item.get("role") or "").strip().lower()
        if role not in READ_SET_ROLES:
            raise ChangeSetError(
                f"Invalid read_set[{index}].role: {role}"
            )
        object_type = str(
            item.get("type") or item.get("kind") or ""
        ).strip().lower()
        if object_type not in READ_SET_TYPES:
            raise ChangeSetError(
                f"Invalid read_set[{index}].type: {object_type}"
            )

        object_id = item.get("id")
        if object_id is not None:
            try:
                object_id = int(object_id)
            except Exception as exc:
                raise ChangeSetError(
                    f"read_set[{index}].id must be an integer"
                ) from exc
            if object_id < 0:
                raise ChangeSetError(
                    f"read_set[{index}].id must be non-negative"
                )

        name = clean_text(
            f"read_set[{index}].name",
            item.get("name"),
            500,
        )
        key = clean_text(
            f"read_set[{index}].key",
            item.get("key"),
            500,
        )
        path = clean_text(
            f"read_set[{index}].path",
            item.get("path"),
            2000,
        )
        if object_type == "file":
            if not path:
                raise ChangeSetError(
                    f"read_set[{index}].path is required for file"
                )
        elif object_id is None and not name and not key:
            raise ChangeSetError(
                f"read_set[{index}] needs id, name or key"
            )

        fingerprint = str(item.get("fingerprint") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ChangeSetError(
                f"read_set[{index}].fingerprint must be sha256 hex"
            )

        fields = sorted(set(string_list(
            f"read_set[{index}].fields",
            item.get("fields"),
            limit=100,
            item_limit=200,
        )))
        identity = (
            role,
            object_type,
            object_id,
            name,
            key,
            path,
        )
        if identity in seen:
            raise ChangeSetError(
                f"Duplicate read_set item at index {index}"
            )
        seen.add(identity)

        result.append({
            "role": role,
            "type": object_type,
            "id": object_id,
            "name": name,
            "key": key,
            "path": path,
            "fingerprint": fingerprint,
            "fields": fields,
        })
    return result


def validate_site_revision(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ChangeSetError("observed_site_revision cannot be boolean")
    if isinstance(value, int):
        if value < 0:
            raise ChangeSetError(
                "observed_site_revision must be non-negative"
            )
        return value
    text = str(value).strip()
    if not text or len(text) > 200:
        raise ChangeSetError("Invalid observed_site_revision")
    return text


def validate_payload(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ChangeSetError("ChangeSet payload must be an object")
    allowed = {
        "summary",
        "mutations",
        "constraints",
        "do_not_change",
        "approval_required",
        "approval_reason",
        "backup",
        "verification",
        "rollback",
        "notes",
        "observed_site_revision",
        "read_set",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ChangeSetError(
            "Unknown ChangeSet payload fields: " + ", ".join(unknown)
        )

    mutations_raw = raw.get("mutations") or []
    if not isinstance(mutations_raw, list):
        raise ChangeSetError("mutations must be a list")
    if not mutations_raw:
        raise ChangeSetError("mutations must contain at least one item")
    if len(mutations_raw) > 50:
        raise ChangeSetError("mutations has too many items (max 50)")
    mutations = [
        validate_mutation(item, index)
        for index, item in enumerate(mutations_raw)
    ]
    ids = [item["id"] for item in mutations]
    if len(ids) != len(set(ids)):
        raise ChangeSetError("Duplicate mutation id")

    approval_required = raw.get("approval_required", False)
    if not isinstance(approval_required, bool):
        raise ChangeSetError("approval_required must be boolean")

    risk_level = max(
        (item["risk"]["level"] for item in mutations),
        key=lambda level: RISK_ORDER[level],
    )
    if (
        risk_level in {"high", "critical"}
        or any(item["action"] == "delete" for item in mutations)
    ):
        approval_required = True

    approval_reason = clean_text(
        "approval_reason",
        raw.get("approval_reason"),
        2000,
    )
    if approval_required and not approval_reason:
        raise ChangeSetError(
            "approval_reason is required when approval_required=true"
        )

    backup = validate_backup(raw.get("backup") or {})
    verification = validate_verification(
        raw.get("verification") or []
    )
    rollback = validate_rollback(raw.get("rollback") or {})

    if any(item["action"] != "create" for item in mutations):
        if not backup["required"] or backup["strategy"] == "none":
            raise ChangeSetError(
                "Existing-target mutations require a real backup strategy"
            )
    if rollback["strategy"] == "none":
        raise ChangeSetError(
            "Mutating ChangeSet requires an explicit rollback strategy"
        )
    if not any(item.get("required") for item in verification):
        raise ChangeSetError(
            "ChangeSet requires at least one required verification check"
        )

    return {
        "observed_site_revision": validate_site_revision(
            raw.get("observed_site_revision")
        ),
        "read_set": validate_read_set(raw.get("read_set")),
        "summary": clean_text(
            "summary",
            raw.get("summary"),
            3000,
            required=True,
        ),
        "mutations": mutations,
        "constraints": string_list(
            "constraints",
            raw.get("constraints"),
            limit=50,
            item_limit=1500,
        ),
        "do_not_change": string_list(
            "do_not_change",
            raw.get("do_not_change"),
            limit=50,
            item_limit=1500,
        ),
        "approval_required": approval_required,
        "approval_reason": approval_reason,
        "risk_level": risk_level,
        "backup": backup,
        "verification": verification,
        "rollback": rollback,
        "notes": clean_text(
            "notes",
            raw.get("notes"),
            4000,
        ),
    }


class ChangeSetStore:
    def __init__(self, site_id: str, cfg: dict):
        self.site_id = site_id
        self.cfg = cfg
        changeset_cfg = cfg.get("changesets") or {}
        if not changeset_cfg.get("enabled", False):
            raise ChangeSetError(
                f"ChangeSet is disabled for site {site_id}"
            )

        manager_root = (cfg.get("paths") or {}).get("manager_root")
        if not manager_root:
            raise ChangeSetError(
                f"paths.manager_root is not configured for site {site_id}"
            )
        self.root = Path(manager_root) / "change-sets"
        self.lock_path = self.root / ".lock"
        ensure_shared_directory(self.root)

        self.task_store = TaskStore(site_id, cfg)
        self.decision_store = DecisionStore(site_id, cfg)
        self.resolver = Resolver(site_id, cfg)
        self.stale_state = StaleStateStore(site_id, cfg)

    @classmethod
    def from_site(cls, site_id: str) -> "ChangeSetStore":
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

    def _path(self, task_id: str) -> Path:
        if not TASK_ID_RE.fullmatch(str(task_id or "")):
            raise ChangeSetError(f"Invalid task id: {task_id}")
        return self.root / f"{task_id}.json"

    def _read_existing(self, task_id: str) -> dict | None:
        path = self._path(task_id)
        if not path.is_file():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ChangeSetError(
                f"Invalid ChangeSet for {task_id}: {exc}"
            ) from exc
        if (
            document.get("site_id") != self.site_id
            or document.get("task_id") != task_id
        ):
            raise ChangeSetError("ChangeSet identity mismatch")
        if document.get("state") not in STATES:
            raise ChangeSetError("Invalid ChangeSet state")
        return document

    def _task(self, task_id: str) -> dict:
        try:
            task = self.task_store.get(task_id)
        except Exception as exc:
            raise ChangeSetError(str(exc)) from exc
        return task

    def _upstream(self, task_id: str) -> tuple[dict, dict, dict, dict]:
        task = self._task(task_id)
        if task.get("status") in TERMINAL_STATUSES:
            raise ChangeSetError(
                f"Cannot plan ChangeSet for terminal task {task_id}"
            )
        try:
            decision = self.decision_store.get(task_id)
            decision_status = self.decision_store.status(task_id)
            resolution = self.resolver.get(task_id)
        except (DecisionError, ResolverError) as exc:
            raise ChangeSetError(str(exc)) from exc

        if not decision_status.get("source_fresh"):
            raise ChangeSetError("DecisionSet source is stale")
        if not decision_status.get("ready_for_changeset"):
            raise ChangeSetError(
                "DecisionSet is not ready for ChangeSet compilation"
            )
        return task, decision, decision_status, resolution


    @staticmethod
    def _resolution_targets(resolution: dict) -> dict[int, dict]:
        result = {}
        for item in resolution.get("targets") or []:
            try:
                index = int(item.get("index"))
            except Exception:
                continue
            result[index] = item
        return result

    @staticmethod
    def _canonical_target(resolved_target: dict) -> dict:
        status = resolved_target.get("status")
        if status == "resolved":
            payload = dict(resolved_target.get("resolved") or {})
        elif status == "new_target":
            payload = dict(resolved_target.get("proposed") or {})
        else:
            payload = {}
        return {
            "index": resolved_target.get("index"),
            "kind": resolved_target.get("kind"),
            "status": status,
            "id": payload.get("id"),
            "name": (
                payload.get("name")
                or payload.get("pagetitle")
                or payload.get("key")
            ),
            "uri": payload.get("uri"),
            "key": payload.get("key"),
            "category": payload.get("category"),
            "static": payload.get("static"),
            "references_in": payload.get("in"),
            "references_out": payload.get("out"),
            "resources": payload.get("resources"),
            "source_id": payload.get("source_id"),
            "source_name": payload.get("source_name"),
            "template_ids": payload.get("template_ids"),
        }

    @staticmethod
    def _expected_action(
        resolution: dict,
        resolved_target: dict,
    ) -> str:
        explicit = str(resolved_target.get("action") or "").strip().lower()
        if explicit == "change":
            explicit = "update"

        operation = str(resolution.get("operation") or "").strip().lower()
        operation_map = {
            "change": "update",
            "create": "create",
            "delete": "delete",
            "move": "move",
            "configure": "configure",
        }
        expected = explicit or operation_map.get(operation)
        if not expected:
            raise ChangeSetError(
                f"TaskSpec operation {operation or 'unknown'} does not "
                "authorize a mutating ChangeSet"
            )

        operation_expected = operation_map.get(operation)
        if (
            explicit
            and operation_expected
            and explicit != operation_expected
        ):
            raise ChangeSetError(
                f"Target action {explicit} conflicts with TaskSpec "
                f"operation {operation}"
            )
        return expected

    @staticmethod
    def _read_set_matches_target(item: dict, mutation: dict) -> bool:
        if item.get("role") != "target":
            return False
        if item.get("type") != mutation.get("kind"):
            return False
        target = mutation.get("target") or {}
        if item.get("id") is not None and target.get("id") is not None:
            return int(item["id"]) == int(target["id"])
        if item.get("name") and target.get("name"):
            return str(item["name"]) == str(target["name"])
        return False

    @staticmethod
    def _auto_target_read_item(mutation: dict) -> dict | None:
        if mutation.get("action") == "create":
            return None
        target = mutation.get("target") or {}
        before = dict(mutation.get("before") or {})
        if not before:
            return None
        fields = sorted(before)
        return {
            "role": "target",
            "type": str(mutation.get("kind") or ""),
            "id": target.get("id"),
            "name": target.get("name"),
            "key": target.get("key"),
            "path": None,
            "fingerprint": canonical_hash(before),
            "fields": fields,
        }

    def _bind_scope(
        self,
        payload: dict,
        resolution: dict,
    ) -> dict:
        targets = self._resolution_targets(resolution)
        bound = []
        used_indexes = set()

        for mutation in payload.get("mutations") or []:
            target_index = mutation["target_index"]
            if target_index in used_indexes:
                raise ChangeSetError(
                    f"Resolved target index {target_index} is mutated more than once"
                )
            used_indexes.add(target_index)

            resolved_target = targets.get(target_index)
            if not resolved_target:
                raise ChangeSetError(
                    f"Mutation {mutation['id']} references unknown "
                    f"ResolvedContext target index {target_index}"
                )
            if resolved_target.get("kind") != mutation.get("kind"):
                raise ChangeSetError(
                    f"Mutation {mutation['id']} kind mismatch: "
                    f"{mutation.get('kind')} != {resolved_target.get('kind')}"
                )

            status = resolved_target.get("status")
            action = mutation.get("action")
            expected_action = self._expected_action(
                resolution,
                resolved_target,
            )
            if action != expected_action:
                raise ChangeSetError(
                    f"Mutation {mutation['id']} action {action} exceeds "
                    f"TaskSpec scope; expected {expected_action}"
                )

            if action == "create":
                if status != "new_target":
                    raise ChangeSetError(
                        f"Create mutation {mutation['id']} requires a "
                        f"new_target ResolvedContext entry, got {status}"
                    )
            elif status != "resolved":
                raise ChangeSetError(
                    f"Mutation {mutation['id']} requires a resolved target, "
                    f"got {status}"
                )

            canonical_target = self._canonical_target(resolved_target)
            before = mutation.get("before") or {}
            if (
                status == "resolved"
                and canonical_target.get("id") is not None
                and before.get("id") is not None
                and int(before.get("id")) != int(canonical_target["id"])
            ):
                raise ChangeSetError(
                    f"Mutation {mutation['id']} before.id does not match "
                    "ResolvedContext target"
                )

            item = dict(mutation)
            item["target"] = canonical_target
            bound.append(item)

        result = dict(payload)
        result["mutations"] = bound

        read_set = [dict(item) for item in (result.get("read_set") or [])]
        # An explicit read-set can incorrectly pair a complete Resolver
        # fingerprint with field-scoped selectors (e.g. ["content"]).
        # Only the same verified *full* fingerprint justifies promoting it
        # to a full-object guard. Never invent or relax a mismatched hash.
        for item in read_set:
            if item.get("role") != "target" or not item.get("fields"):
                continue
            matching = [
                mutation
                for mutation in bound
                if self._read_set_matches_target(item, mutation)
            ]
            if len(matching) != 1:
                continue
            target_resolution = targets.get(
                matching[0].get("target_index")
            ) or {}
            live = target_resolution.get("live") or {}
            full_fingerprint = str(live.get("fingerprint") or "").lower()
            if (
                live.get("status") == "verified"
                and full_fingerprint
                and full_fingerprint == str(item.get("fingerprint") or "").lower()
            ):
                item["fields"] = []

        for item in read_set:
            if (
                item.get("type") in {
                    "resource", "template", "chunk", "snippet", "plugin",
                    "tv", "category",
                }
                and "content" in (item.get("fields") or [])
            ):
                raise ChangeSetError(
                    "MODX read_set.fields cannot include raw 'content': "
                    "use 'content_sha256' with its projected fingerprint, "
                    "or fields=[] with an authoritative full fingerprint. "
                    "The mutation.fields list is separate."
                )

        for mutation in bound:
            if mutation.get("action") == "create":
                continue
            if not any(
                self._read_set_matches_target(item, mutation)
                for item in read_set
            ):
                generated = self._auto_target_read_item(mutation)
                resolved_source = targets.get(
                    mutation.get("target_index")
                ) or {}
                live_fingerprint = (
                    (resolved_source.get("live") or {}).get(
                        "fingerprint"
                    )
                )
                if generated and live_fingerprint:
                    generated["fingerprint"] = str(
                        live_fingerprint
                    ).lower()
                    generated["fields"] = []
                if generated:
                    read_set.append(generated)
        result["read_set"] = read_set
        if result.get("observed_site_revision") is None:
            result["observed_site_revision"] = resolution.get(
                "observed_site_revision"
            )

        shared = [
            item
            for item in bound
            if int((item.get("target") or {}).get("references_in") or 0) > 1
        ]
        if shared:
            result["approval_required"] = True
            shared_text = ", ".join(
                f"{item['kind']} "
                f"#{(item.get('target') or {}).get('id')} "
                f"{(item.get('target') or {}).get('name') or ''} "
                f"(references_in="
                f"{(item.get('target') or {}).get('references_in')})"
                for item in shared
            )
            reason = (
                "Shared target requires explicit approval: "
                + shared_text
            )
            if result.get("approval_reason"):
                if reason not in result["approval_reason"]:
                    result["approval_reason"] = (
                        result["approval_reason"] + " " + reason
                    )
            else:
                result["approval_reason"] = reason

        result["scope_fingerprint"] = canonical_hash({
            "mutations": [
                {
                    "id": item["id"],
                    "target_index": item["target_index"],
                    "kind": item["kind"],
                    "action": item["action"],
                    "target": item["target"],
                }
                for item in bound
            ],
            "do_not_change": result.get("do_not_change") or [],
        })
        result["approval_fingerprint"] = canonical_hash({
            "summary": result.get("summary"),
            "mutations": [
                {
                    "id": item.get("id"),
                    "target_index": item.get("target_index"),
                    "action": item.get("action"),
                    "kind": item.get("kind"),
                    "target": item.get("target"),
                    "after": item.get("after"),
                    "fields": item.get("fields"),
                    "dependencies": item.get("dependencies"),
                    "rationale": item.get("rationale"),
                    "risk": item.get("risk"),
                }
                for item in bound
            ],
            "verification": result.get("verification") or [],
            "rollback": result.get("rollback") or {},
            "backup": result.get("backup") or {},
            "risk_level": result.get("risk_level"),
            "approval_required": result.get("approval_required"),
            "approval_reason": result.get("approval_reason"),
            "do_not_change": result.get("do_not_change") or [],
        })
        result["payload_fingerprint"] = canonical_hash({
            key: value
            for key, value in result.items()
            if key not in {"payload_fingerprint"}
        })
        return result

    @staticmethod
    def _source_from(
        decision: dict,
        resolution: dict,
    ) -> dict:
        decision_source = decision.get("source") or {}
        return {
            "decision_revision": decision.get("revision"),
            "decision_updated_at": decision.get("updated_at"),
            "resolution_fingerprint": resolution.get("fingerprint"),
            "spec_revision": resolution.get("spec_revision"),
            "inventory_fingerprint": resolution.get("inventory_fingerprint"),
            "architecture_fingerprint": resolution.get(
                "architecture_fingerprint"
            ),
            "decision_source_resolution_fingerprint": decision_source.get(
                "resolution_fingerprint"
            ),
        }

    def _source_fresh(self, document: dict) -> tuple[bool, list[str]]:
        reasons = []
        task_id = document["task_id"]
        try:
            task = self.task_store.get(task_id)
            if task.get("status") in TERMINAL_STATUSES:
                reasons.append("task_terminal")
        except Exception:
            reasons.append("task_unavailable")

        try:
            decision_status = self.decision_store.status(task_id)
            if not decision_status.get("available"):
                reasons.append("decision_unavailable")
            if not decision_status.get("source_fresh"):
                reasons.append("decision_stale")
            if not decision_status.get("ready_for_changeset"):
                reasons.append("decision_not_ready")
            source = document.get("source") or {}
            if (
                source.get("decision_revision")
                != decision_status.get("revision")
            ):
                reasons.append("decision_changed")
        except Exception:
            reasons.append("decision_unavailable")

        if self.stale_state.invalidates(
            task_id,
            document.get("updated_at"),
        ):
            reasons.append("stale_state_invalidated")

        state = document.get("state")
        payload = document.get("payload") or {}
        frozen = document.get("frozen") or {}
        if state in {"sealed", "approved"}:
            expected = frozen.get("payload_fingerprint")
            current = payload.get("payload_fingerprint")
            computed = canonical_hash({
                key: value for key, value in payload.items()
                if key != "payload_fingerprint"
            })
            if not expected or expected != current or current != computed:
                reasons.append("frozen_payload_changed")
        if state == "approved":
            # No trusted approval provider exists in this extracted component.
            reasons.append("approval_provenance_unverified")

        return not reasons, sorted(set(reasons))


    def _document_status(self, document: dict) -> dict:
        source_fresh, stale_reasons = self._source_fresh(document)
        payload = document.get("payload") or {}
        state = document.get("state")
        approval_required = bool(payload.get("approval_required"))
        ready_for_execution = (
            source_fresh and state == "sealed" and not approval_required
        )
        return {
            "source_fresh": source_fresh,
            "stale_reasons": stale_reasons,
            "ready_for_execution": ready_for_execution,
        }

    def get(self, task_id: str) -> dict:
        document = self._read_existing(task_id)
        if not document:
            raise ChangeSetError(f"No ChangeSet for task {task_id}")
        result = dict(document)
        result.update(self._document_status(document))
        return result

    def status(self, task_id: str) -> dict:
        path = self._path(task_id)
        if not path.is_file():
            return {
                "site_id": self.site_id,
                "task_id": task_id,
                "available": False,
                "state": None,
                "source_fresh": False,
                "ready_for_execution": False,
                "path": str(path),
            }
        document = self.get(task_id)
        payload = document.get("payload") or {}
        return {
            "site_id": self.site_id,
            "task_id": task_id,
            "available": True,
            "revision": document.get("revision"),
            "state": document.get("state"),
            "source_fresh": document.get("source_fresh"),
            "stale_reasons": document.get("stale_reasons"),
            "ready_for_execution": document.get("ready_for_execution"),
            "mutation_count": len(payload.get("mutations") or []),
            "risk_level": payload.get("risk_level"),
            "approval_required": payload.get("approval_required"),
            "scope_fingerprint": payload.get("scope_fingerprint"),
            "payload_fingerprint": payload.get("payload_fingerprint"),
            "approval_fingerprint": payload.get("approval_fingerprint"),
            "observed_site_revision": payload.get("observed_site_revision"),
            "read_set_count": len(payload.get("read_set") or []),
            "path": str(path),
        }

    @staticmethod
    def _scope_diff(old_payload: dict, new_payload: dict) -> dict:
        def rows(payload):
            return {
                item["id"]: {
                    "target_index": item.get("target_index"),
                    "kind": item.get("kind"),
                    "action": item.get("action"),
                    "target": item.get("target"),
                }
                for item in payload.get("mutations") or []
            }

        old = rows(old_payload)
        new = rows(new_payload)
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        changed = sorted(
            key
            for key in set(old) & set(new)
            if old[key] != new[key]
        )
        return {
            "added": added,
            "removed": removed,
            "changed": changed,
            "scope_expanded": bool(added or changed),
        }

    def _save_draft(
        self,
        task_id: str,
        raw_payload: dict,
        *,
        source_kind: str,
        amendment_reason: str | None = None,
        amendment_source: str | None = None,
    ) -> dict:
        _, decision, _, resolution = self._upstream(task_id)
        payload = self._bind_scope(
            validate_payload(raw_payload),
            resolution,
        )

        existing = self._read_existing(task_id)
        if (
            existing
            and existing.get("state") in {"sealed", "approved"}
            and source_kind != "amend"
        ):
            raise ChangeSetError(
                "Frozen ChangeSet cannot be overwritten; use amend"
            )

        revision = int((existing or {}).get("revision") or 0) + 1
        now = utc_now()
        history = list((existing or {}).get("history") or [])
        scope_diff = None
        if source_kind == "amend":
            if not existing:
                raise ChangeSetError("Cannot amend a missing ChangeSet")
            amendment_reason = clean_text(
                "amendment_reason",
                amendment_reason,
                3000,
                required=True,
            )
            scope_diff = self._scope_diff(
                existing.get("payload") or {},
                payload,
            )
            history.append({
                "at": now,
                "action": "amend",
                "from_revision": existing.get("revision"),
                "from_state": existing.get("state"),
                "from_payload_fingerprint": (
                    (existing.get("payload") or {}).get(
                        "payload_fingerprint"
                    )
                ),
                "reason": amendment_reason,
                "source": amendment_source,
                "scope_diff": scope_diff,
            })
        else:
            history.append({
                "at": now,
                "action": "set",
                "revision": revision,
                "source": source_kind,
            })

        # This repository intentionally has no independent trusted approver.
        # A changed plan always returns to draft, even if an imported record
        # claims a prior approval. Never reuse authority based on a fingerprint.
        document = {
            "format": 1,
            "site_id": self.site_id,
            "task_id": task_id,
            "revision": revision,
            "state": "draft",
            "created_at": (existing or {}).get("created_at") or now,
            "updated_at": now,
            "source": self._source_from(decision, resolution),
            "payload": payload,
            "frozen": None,
            "approval": None,
            "history": history[-100:],
        }
        if scope_diff is not None:
            document["last_scope_diff"] = scope_diff

        with self._locked():
            current = self._read_existing(task_id)
            # Optimistic concurrency control: reject a draft prepared from a
            # snapshot already replaced by another writer or sealing action.
            # Sealing a plan does not necessarily increment revision. Compare
            # the entire previously observed document, not revision alone, to
            # prevent a concurrent seal/amend from being overwritten.
            if current != existing:
                raise ChangeSetError("Concurrent ChangeSet modification; reload and retry")
            self._atomic_json(self._path(task_id), document)
        return self.get(task_id)

    def set(
        self,
        task_id: str,
        raw_payload: dict,
        *,
        source: str = "agent",
    ) -> dict:
        source = clean_text("source", source, 200, required=True)
        if source == "amend":
            raise ChangeSetError("Use amend() to modify a frozen ChangeSet")
        return self._save_draft(
            task_id,
            raw_payload,
            source_kind=source,
        )

    def amend(
        self,
        task_id: str,
        raw_payload: dict,
        *,
        reason: str,
        source: str = "agent",
    ) -> dict:
        source = clean_text("source", source, 200, required=True)
        return self._save_draft(
            task_id,
            raw_payload,
            source_kind="amend",
            amendment_reason=reason,
            amendment_source=source,
        )

    def _freeze(
        self,
        task_id: str,
        *,
        state: str,
        reason: str,
        source: str,
    ) -> dict:
        if state != "sealed":
            raise ChangeSetError("Only sealing is available; trusted approval is not implemented")
        reason = clean_text(
            "reason",
            reason,
            3000,
            required=True,
        )
        source = clean_text(
            "source",
            source,
            200,
            required=True,
        )

        with self._locked():
            document = self._read_existing(task_id)
            if not document:
                raise ChangeSetError("ChangeSet disappeared before freeze")
            if document.get("state") != "draft":
                raise ChangeSetError(
                    f"ChangeSet is already {document.get('state')}"
                )
            # Read, validate freshness, and seal the SAME document under the
            # lock. An overlapping writer must not replace what was reviewed.
            if not self._source_fresh(document)[0]:
                raise ChangeSetError("Cannot freeze a stale ChangeSet")
            payload = document.get("payload") or {}
            # Validate the draft that is actually being sealed, under the
            # same lock as the state change. A post-seal status check is too
            # late: an altered draft must not become sealed at all.
            stored_fingerprint = payload.get("payload_fingerprint")
            computed_fingerprint = canonical_hash({
                key: value for key, value in payload.items()
                if key != "payload_fingerprint"
            })
            if (
                not isinstance(stored_fingerprint, str)
                or stored_fingerprint != computed_fingerprint
            ):
                raise ChangeSetError(
                    "ChangeSet draft payload fingerprint mismatch; sealing denied"
                )
            approval_required = bool(payload.get("approval_required"))
            if approval_required:
                raise ChangeSetError("Explicit approval is required; sealing denied")

            now = utc_now()
            document["state"] = state
            document["updated_at"] = now
            document["frozen"] = {
                "at": now,
                "payload_fingerprint": payload.get(
                    "payload_fingerprint"
                ),
                "scope_fingerprint": payload.get(
                    "scope_fingerprint"
                ),
                "approval_fingerprint": payload.get(
                    "approval_fingerprint"
                ),
            }
            document["approval"] = {
                "required": approval_required,
                "status": (
                    "approved"
                    if state == "approved"
                    else "not_required"
                ),
                "at": now,
                "source": source,
                "reason": reason,
            }
            history = list(document.get("history") or [])
            history.append({
                "at": now,
                "action": state,
                "source": source,
                "reason": reason,
                "revision": document.get("revision"),
                "payload_fingerprint": payload.get(
                    "payload_fingerprint"
                ),
            })
            document["history"] = history[-100:]
            self._atomic_json(self._path(task_id), document)

        return self.get(task_id)

    def seal(
        self,
        task_id: str,
        *,
        reason: str,
        source: str = "policy",
    ) -> dict:
        return self._freeze(
            task_id,
            state="sealed",
            reason=reason,
            source=source,
        )

    def approve(
        self,
        task_id: str,
        *,
        reason: str,
        source: str = "user",
    ) -> dict:
        # The caller controls `source`: `source="user"` does NOT authenticate
        # a human approval. Never manufacture that authority in a public demo.
        raise ChangeSetError(
            "Approval unavailable: an independent trusted approver is required"
        )
