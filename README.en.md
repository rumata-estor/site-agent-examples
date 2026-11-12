**Язык / Language:** [Русский](README.md) | [English](README.en.md)

# Site Agent Runtime — Engineering Design Examples

Three components from the private Site Agent Runtime project: source code, tests, and interactive demos. This is **not the complete Runtime** or a ready-to-use product.

| Component | Source code | Public version |
| --- | --- | --- |
| Task Manager | [`siteagent/tasks.py`](siteagent/tasks.py) | Active-task pointer consistency fixed; tests added. |
| ChangeSet | [`siteagent/changesets.py`](siteagent/changesets.py) | Protection against races and draft tampering; untrusted approvals are rejected. Tests and a demo added. |
| State Guard | [`siteagent/state_guard.py`](siteagent/state_guard.py) | Concurrent marker writes and integrity checks fixed; tests and a demo added. |

ChangeSet reads task state from Task Manager, decision data through DecisionStore, and permitted targets and operations from Resolver. State Guard stores stale-state markers that ChangeSet checks before sealing a plan. The complete TaskSpec, DecisionSet, and Resolver implementations are not included; the tests use synthetic dependencies.

## Task Manager: Engineering Design

Task Manager stores task state separately from the conversation: lifecycle, allowed transitions, checkpoints, and a results log. Records are stored as JSON using atomic file replacement; operations are serialized with `fcntl.flock`.

A task record and `active.json` cannot be updated in one atomic operation. If the process fails between the two writes, the active-task pointer may become stale. The pointer is therefore recovered from task records while holding a lock:

- If exactly one task is in `running` or `verifying`, it becomes the active task.
- Two busy tasks or a corrupted record cause an explicit error rather than an arbitrary selection.
- If no task is busy, a `paused` or `blocked` task may remain selected; references to completed or deleted tasks are cleared.
- A second busy task cannot be created while another one exists.

Recovery requires scanning stored tasks. The design targets a small task directory and prioritizes correctness.

## ChangeSet: Engineering Design

ChangeSet describes the objects to modify, expected results, checks, backup, and rollback. A plan is validated and bound to the targets and operations allowed by Resolver in the TaskSpec scope; a canonical SHA-256 fingerprint is then computed. A sealed plan cannot be overwritten through `set()`: changes must use `amend()`, which returns it to draft state.

The original implementation had race conditions between preparing, modifying, and sealing a plan. In the public version, a draft write checks **under a file lock** that the previously read document has not changed. Before `seal()`, the same lock protects checks of source freshness, the draft's content against its stored SHA-256 fingerprint, and whether approval is required. Tampering with an already sealed plan is detected when its status is checked.

This repository has no trusted authorization mechanism: `approve()` always rejects requests, even with `source="user"`. Only plans that do not require approval can be finalized. An `amend` does not inherit a previous approval. **No plans are executed here.**

## State Guard: Engineering Design

`canonical_hash` computes SHA-256 over a canonical JSON representation. `StaleStateStore` maintains a `STALE_STATE` marker with a revision number and diagnostic details to determine whether data was marked stale after it was read. Another component must create the marker: State Guard does not monitor changes to a live website on its own.

State Guard **does not perform compare-and-swap in the CMS**. The executing adapter must reject writes based on stale versions; the browser demo uses a synthetic adapter.

In the original implementation, two concurrent calls could write the same revision number. In the public version, reading and incrementing the revision are protected by `fcntl.flock`. Corrupted markers and markers with mismatched identities are not silently overwritten. Timestamps are compared with timezone offsets taken into account; invalid timestamps are rejected.

## Interactive Browser Demo

**[▶ Open the interactive demos on GitHub Pages](https://rumata-estor.github.io/site-agent-examples/index.en.html)**

Three tabs, each with three sequential actions:

- **Task Manager:** start a task → try to start a second one (rejected) → complete the first.
- **ChangeSet:** prepare a regular plan → check a high-risk plan → try to approve it without trusted authorization (rejected).
- **State Guard:** two agents read the same text → agent A changes it → agent B tries to write an outdated version (rejected, with a `STALE_STATE` marker).

After each action, the interface shows the state, outcome, and reason for rejection. Scenarios can be restarted with the “Start over” button. Links to source code and test results are available in the interface.

The browser runs the published Python modules through Pyodide with synthetic dependencies; it does not connect to real websites. State Guard version checks are performed by a test adapter, not a real CMS. Interprocess `fcntl.flock` behavior is tested on Linux / GitHub Actions, not in Pyodide.

## Tests on GitHub

[**View automated test results in GitHub Actions**](../../actions/workflows/task-manager-tests.yml)

The tests cover:

- **Task Manager:** state transitions, checkpoints, prevention of a second active task, crash recovery, corrupted records, and concurrent task creation across threads and processes.
- **ChangeSet:** TaskSpec scope limits, secret fields, high-risk changes, rejection of forged approval, stale plans, rejection of tampered drafts before sealing, detection of sealed-plan tampering, `amend`, and concurrent writes.
- **State Guard:** canonical fingerprints, stale state, timestamps with different UTC offsets, revisions, concurrent writes across threads and processes, and corrupted markers.

Run locally on Linux with Python 3.10–3.13; no third-party packages are required:

```bash
git clone https://github.com/rumata-estor/site-agent-examples.git
cd site-agent-examples
python3 -m unittest discover -s tests -v
```

The tests exercise the real `TaskStore`, `ChangeSetStore`, and `StaleStateStore` logic, including file operations. Missing integrations are replaced with synthetic `DecisionStore`, `Resolver`, and environment helpers; selected race-condition tests instrument document reads. GitHub Actions tests the Python components on Linux with Python 3.10–3.13, including an independent run of each test module, but does not test the browser interface.

## Security Boundaries

The published components do not control live websites: they do not include the complete Executor or an independent authorization controller. ChangeSet does not accept human approval without a trusted mechanism. SHA-256 fingerprints do not protect the file store against direct modifications by an attacker with write access. **Do not use this code as a ready-made system for safely executing changes.**

No independent security audit has been performed.

The source code is published solely for studying the architecture, demonstrating engineering decisions, and noncommercial testing. This is **not Open Source** or a ready-to-use software product. Use is governed by the [PolyForm Strict License 1.0.0](LICENSE), which permits noncommercial use but does not grant permission to modify or redistribute the software.
