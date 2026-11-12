"""Actual StaleStateStore under local filesystem, including multiprocessing."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from tempfile import TemporaryDirectory
import json
import time
import unittest

from tests._runtime_stubs import install_environment_stubs

install_environment_stubs()
from siteagent.state_guard import canonical_hash, utc_now, StaleStateStore, StateGuardError

TASK_ID = 'TASK-20261112-091100-aaaa'


def _process_mark(manager_root, barrier, result_queue, index):
    store = StaleStateStore('example', {'paths': {'manager_root': manager_root}})
    barrier.wait(timeout=10)
    try:
        record = store.mark(TASK_ID, {'source': 'synthetic-process', 'id': index})
        result_queue.put(('ok', record['revision']))
    except Exception as exc:
        result_queue.put(('fail', type(exc).__name__))


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix='site-agent-guard-')
        self.cfg = {'paths': {'manager_root': self.tmp.name}}
        self.guard = StaleStateStore('example', self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_canonical_hash_is_order_independent(self):
        self.assertEqual(canonical_hash({'b': 2, 'a': {'x': 1}}),
                         canonical_hash({'a': {'x': 1}, 'b': 2}))
        self.assertNotEqual(canonical_hash({'a': 1}), canonical_hash({'a': 2}))

    def test_missing_marker(self):
        self.assertIsNone(self.guard.get(TASK_ID))
        self.assertFalse(self.guard.invalidates(TASK_ID, '2025-01-01T00:00:00+00:00'))

    def test_invalidation_only_for_older_snapshot(self):
        before = utc_now()
        time.sleep(0.002)
        marker = self.guard.mark(TASK_ID, {'reason': 'synthetic-data-changed'})
        time.sleep(0.002)
        after = utc_now()
        self.assertEqual(marker['revision'], 1)
        self.assertEqual(marker['status'], 'STALE_STATE')
        self.assertTrue(self.guard.invalidates(TASK_ID, before))
        self.assertFalse(self.guard.invalidates(TASK_ID, after))
        self.assertTrue(self.guard.invalidates(TASK_ID, None))

    def test_timestamps_with_different_offsets_are_compared_chronologically(self):
        marker = self.guard.mark(TASK_ID, {'reason': 'changed'})
        marker['at'] = '2026-11-12T08:00:00+00:00'
        self.guard._path(TASK_ID).write_text(json.dumps(marker), encoding='utf-8')

        # 10:00 at +03:00 is 07:00 UTC, so the observation is stale.
        self.assertTrue(self.guard.invalidates(TASK_ID, '2026-11-12T10:00:00+03:00'))
        # 04:00 at -05:00 is 09:00 UTC, after the marker.
        self.assertFalse(self.guard.invalidates(TASK_ID, '2026-11-12T04:00:00-05:00'))
        self.assertTrue(self.guard.invalidates(TASK_ID, '2026-11-12T07:00:00Z'))
        self.assertFalse(self.guard.invalidates(TASK_ID, '2026-11-12T09:00:00Z'))

    def test_untrusted_observation_timestamps_are_rejected(self):
        self.guard.mark(TASK_ID, {'reason': 'changed'})
        for observed in ['not-a-time', '2026-11-12T08:00:00', '  ']:
            with self.subTest(observed=observed):
                with self.assertRaises(StateGuardError):
                    self.guard.invalidates(TASK_ID, observed)

    def test_invalid_marker_fields_fail_closed_without_overwriting(self):
        original = self.guard.mark(TASK_ID, {'reason': 'original'})
        cases = [
            ('missing timestamp', lambda x: x.pop('at')),
            ('unparseable timestamp', lambda x: x.update(at='not-a-time')),
            ('naive timestamp', lambda x: x.update(at='2026-11-12T08:00:00')),
            ('missing revision', lambda x: x.pop('revision')),
            ('zero revision', lambda x: x.update(revision=0)),
            ('boolean revision', lambda x: x.update(revision=True)),
            ('string revision', lambda x: x.update(revision='1')),
            ('wrong diagnostic type', lambda x: x.update(diagnostic='bad')),
            ('missing format', lambda x: x.pop('format')),
            ('wrong format', lambda x: x.update(format=2)),
        ]
        for name, corrupt in cases:
            with self.subTest(name=name):
                value = dict(original)
                corrupt(value)
                path = self.guard._path(TASK_ID)
                path.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(StateGuardError):
                    self.guard.get(TASK_ID)
                with self.assertRaises(StateGuardError):
                    self.guard.invalidates(TASK_ID, '2026-11-12T07:00:00+00:00')
                with self.assertRaises(StateGuardError):
                    self.guard.mark(TASK_ID, {'reason': 'do not overwrite'})
                self.assertEqual(json.loads(path.read_text(encoding='utf-8')), value)

    def test_revision_increments_after_reopen(self):
        self.guard.mark(TASK_ID, {'reason': 'first'})
        reopened = StaleStateStore('example', self.cfg)
        record = reopened.mark(TASK_ID, {'reason': 'second'})
        self.assertEqual(record['revision'], 2)
        self.assertEqual(self.guard.get(TASK_ID)['revision'], 2)

    def test_concurrent_threads_do_not_lose_revisions(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            revisions = list(pool.map(lambda i: self.guard.mark(TASK_ID, {'index': i})['revision'], range(40)))
        self.assertEqual(sorted(revisions), list(range(1, 41)))
        self.assertEqual(self.guard.get(TASK_ID)['revision'], 40)

    def test_concurrent_processes_do_not_lose_revisions(self):
        ctx = get_context('fork')
        barrier = ctx.Barrier(5)
        queue = ctx.Queue()
        workers = [ctx.Process(target=_process_mark,
                               args=(self.tmp.name, barrier, queue, i)) for i in range(5)]
        for proc in workers:
            proc.start()
        try:
            results = [queue.get(timeout=10) for _ in workers]
        finally:
            for proc in workers:
                proc.join(timeout=10)
                if proc.is_alive():
                    proc.terminate()
                    proc.join()
        self.assertTrue(all(proc.exitcode == 0 for proc in workers))
        self.assertTrue(all(kind == 'ok' for kind, _ in results))
        self.assertEqual(sorted(value for _, value in results), list(range(1, 6)))
        self.assertEqual(self.guard.get(TASK_ID)['revision'], 5)

    def test_corrupt_marker_fails_closed(self):
        path = self.guard._path(TASK_ID)
        path.write_text('{invalid JSON', encoding='utf-8')
        with self.assertRaisesRegex(StateGuardError, 'Invalid stale-state marker'):
            self.guard.mark(TASK_ID, {'reason': 'should not reset revision'})

    def test_identity_mismatch_fails_closed(self):
        self.guard.mark(TASK_ID, {'reason': 'original'})
        path = self.guard._path(TASK_ID)
        value = self.guard.get(TASK_ID)
        value['site_id'] = 'other-site'
        path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaisesRegex(StateGuardError, 'identity mismatch'):
            self.guard.get(TASK_ID)
        with self.assertRaises(StateGuardError):
            self.guard.mark(TASK_ID, {'reason': 'should not replace'})

    def test_invalid_identifier_rejected(self):
        with self.assertRaises(StateGuardError):
            self.guard.mark('../escape', {'reason': 'bad id'})


if __name__ == '__main__':
    unittest.main()
