"""ChangeSet contract tests. Synthetic upstream fixtures, original ChangeSet logic."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
import copy
import sys
import threading
import types
import unittest

from tests._runtime_stubs import install_environment_stubs

install_environment_stubs()
fake_decisions = types.ModuleType('siteagent.decisions')
class DecisionError(RuntimeError): pass
class DecisionStore:
    def __init__(self, site_id, cfg):
        self.decision = cfg['fixture']['decision']
    def get(self, task_id): return self.decision
    def status(self, task_id):
        return {'available': True, 'revision': self.decision['revision'],
                'source_fresh': True, 'ready_for_changeset': True}
fake_decisions.DecisionError = DecisionError
fake_decisions.DecisionStore = DecisionStore
sys.modules.setdefault('siteagent.decisions', fake_decisions)

fake_resolver = types.ModuleType('siteagent.resolver')
class ResolverError(RuntimeError): pass
class Resolver:
    def __init__(self, site_id, cfg): self.resolution = cfg['fixture']['resolution']
    def get(self, task_id): return self.resolution
fake_resolver.ResolverError = ResolverError
fake_resolver.Resolver = Resolver
sys.modules.setdefault('siteagent.resolver', fake_resolver)

# Use the real State Guard implementation: only DecisionSet/Resolver are synthetic.
from siteagent.tasks import TaskStore
from siteagent.changesets import ChangeSetStore, ChangeSetError


def fixture_payload(text='New content', risk='low', action='update', secret=False):
    after = {'id': 1, 'content': text}
    if secret: after['api_key'] = 'TEST-VALUE-ONLY'
    return {
        'summary': 'Modify a synthetic page fragment',
        'mutations': [{
            'id': 'update-chunk', 'target_index': 0, 'action': action, 'kind': 'chunk',
            'before': {'id': 1, 'content': 'Original content'},
            'after': after,
            'fields': ['content'], 'dependencies': [],
            'rationale': 'Check the permitted scope', 'risk': {'level': risk, 'reasons': ['Demo']},
        }],
        'approval_required': risk in {'high', 'critical'},
        'approval_reason': 'User review is required' if risk in {'high','critical'} else None,
        'backup': {'required': True, 'strategy': 'element_snapshot', 'scope': ['chunk#1'],
                   'reason': 'Restore the original text'},
        'verification': [{'id': 'readback', 'type': 'cms_read', 'target': 'chunk#1',
                          'expectation': 'Read back', 'required': True}],
        'rollback': {'strategy': 'restore_snapshot', 'steps': ['Restore prior content'],
                     'reason': 'Rollback', 'automatic_on_failure': False},
    }


class ChangeSetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix='site-agent-changeset-')
        resolution_fp = 'synthetic-resolution'
        self.cfg = {
            'paths': {'manager_root': self.tmp.name},
            'task_manager': {'enabled': True},
            'changesets': {'enabled': True},
            'fixture': {
                'decision': {'revision': 1, 'updated_at': '2026-01-01T00:00:00+00:00',
                             'source': {'resolution_fingerprint': resolution_fp}},
                'resolution': {'fingerprint': resolution_fp, 'operation': 'change',
                               'spec_revision': 1, 'inventory_fingerprint': 'test-inventory',
                               'architecture_fingerprint': 'test-architecture',
                               'observed_site_revision': 1,
                               'targets': [{'index': 0, 'kind': 'chunk', 'status': 'resolved',
                                            'action': 'update',
                                            'resolved': {'id': 1, 'name': 'demo_chunk', 'in': 1}}]},
            },
        }
        self.store = ChangeSetStore('example', self.cfg)
        self.task_id = self.store.task_store.create('Check an update')['id']

    def tearDown(self): self.tmp.cleanup()

    def test_valid_draft_and_seal(self):
        draft = self.store.set(self.task_id, fixture_payload())
        self.assertEqual(draft['state'], 'draft')
        self.assertEqual(len(draft['payload']['read_set']), 1)
        sealed = self.store.seal(self.task_id, reason='Reviewed simple plan')
        self.assertEqual(sealed['state'], 'sealed')
        self.assertTrue(sealed['source_fresh'])
        self.assertTrue(sealed['ready_for_execution'])

    def test_scope_mismatch_rejected(self):
        payload = fixture_payload(action='delete')
        payload['mutations'][0]['after'] = {'exists': False}
        payload['approval_reason'] = 'Deletion must be reviewed'
        with self.assertRaisesRegex(ChangeSetError, 'exceeds TaskSpec scope'):
            self.store.set(self.task_id, payload)

    def test_sensitive_values_rejected(self):
        with self.assertRaisesRegex(ChangeSetError, 'sensitive field'):
            self.store.set(self.task_id, fixture_payload(secret=True))

    def test_high_risk_cannot_be_sealed_or_forge_approval(self):
        self.store.set(self.task_id, fixture_payload(risk='high'))
        with self.assertRaisesRegex(ChangeSetError, 'approval'):
            self.store.seal(self.task_id, reason='Ignore requirement')
        with self.assertRaisesRegex(ChangeSetError, 'trusted approver'):
            self.store.approve(self.task_id, reason='I am the user', source='user')
        self.assertFalse(self.store.status(self.task_id)['ready_for_execution'])

    def test_shared_element_requires_approval(self):
        self.cfg['fixture']['resolution']['targets'][0]['resolved']['in'] = 4
        draft = self.store.set(self.task_id, fixture_payload())
        self.assertTrue(draft['payload']['approval_required'])
        with self.assertRaises(ChangeSetError):
            self.store.seal(self.task_id, reason='Attempt bypass')

    def test_cannot_replace_frozen_draft_without_amend(self):
        self.store.set(self.task_id, fixture_payload())
        self.store.seal(self.task_id, reason='Reviewed')
        with self.assertRaisesRegex(ChangeSetError, 'use amend'):
            self.store.set(self.task_id, fixture_payload(text='Another'))
        amended = self.store.amend(self.task_id, fixture_payload(text='Another'),
                                   reason='Change specification', source='reviewer')
        self.assertEqual(amended['state'], 'draft')
        self.assertIsNone(amended['approval'])
        self.assertEqual(amended['history'][-1]['source'], 'reviewer')
        self.assertEqual(amended['revision'], 2)
        self.assertTrue(self.store.seal(self.task_id, reason='Re-reviewed')['ready_for_execution'])

    def test_reserved_amend_source_is_not_a_bypass(self):
        self.store.set(self.task_id, fixture_payload())
        self.store.seal(self.task_id, reason='Reviewed')
        with self.assertRaises(ChangeSetError):
            self.store.set(self.task_id, fixture_payload(text='New'), source='amend')

    def test_stale_source_blocks_freeze(self):
        self.store.set(self.task_id, fixture_payload())
        self.store.stale_state.mark(self.task_id, {'reason': 'source_changed'})
        with self.assertRaisesRegex(ChangeSetError, 'stale'):
            self.store.seal(self.task_id, reason='Try to freeze old plan')
        self.assertFalse(self.store.get(self.task_id)['source_fresh'])

    def test_tampered_draft_cannot_be_sealed(self):
        self.store.set(self.task_id, fixture_payload())
        path = self.store._path(self.task_id)
        doc = self.store._read_existing(self.task_id)
        doc['payload']['mutations'][0]['after']['content'] = 'Tampered before seal'
        self.store._atomic_json(path, doc)

        with self.assertRaisesRegex(ChangeSetError, 'fingerprint mismatch'):
            self.store.seal(self.task_id, reason='Do not finalize altered draft')

        self.assertEqual(self.store._read_existing(self.task_id)['state'], 'draft')
        self.assertFalse(self.store.status(self.task_id)['ready_for_execution'])

    def test_missing_draft_fingerprint_cannot_be_sealed(self):
        self.store.set(self.task_id, fixture_payload())
        path = self.store._path(self.task_id)
        doc = self.store._read_existing(self.task_id)
        doc['payload'].pop('payload_fingerprint')
        self.store._atomic_json(path, doc)

        with self.assertRaisesRegex(ChangeSetError, 'fingerprint mismatch'):
            self.store.seal(self.task_id, reason='Do not finalize unverified draft')

        self.assertEqual(self.store._read_existing(self.task_id)['state'], 'draft')

    def test_frozen_payload_recalculated(self):
        self.store.set(self.task_id, fixture_payload())
        self.store.seal(self.task_id, reason='Reviewed')
        path = self.store._path(self.task_id)
        doc = self.store._read_existing(self.task_id)
        doc['payload']['mutations'][0]['after']['content'] = 'Tampered'
        self.store._atomic_json(path, doc)
        status = self.store.status(self.task_id)
        self.assertFalse(status['source_fresh'])
        self.assertIn('frozen_payload_changed', status['stale_reasons'])
        self.assertFalse(status['ready_for_execution'])

    def test_forged_approved_record_is_untrusted(self):
        self.store.set(self.task_id, fixture_payload(risk='high'))
        doc = self.store._read_existing(self.task_id)
        doc['state'] = 'approved'
        doc['frozen'] = {'payload_fingerprint': doc['payload']['payload_fingerprint']}
        self.store._atomic_json(self.store._path(self.task_id), doc)
        self.assertFalse(self.store.status(self.task_id)['ready_for_execution'])
        self.assertIn('approval_provenance_unverified', self.store.get(self.task_id)['stale_reasons'])

    def test_concurrent_seal_cannot_be_overwritten_by_stale_draft(self):
        self.store.set(self.task_id, fixture_payload(text='Original proposed text'))
        read = self.store._read_existing
        intervened = {'value': False}

        def race(task_id):
            snapshot = read(task_id)
            if not intervened['value']:
                intervened['value'] = True
                # Simulate a seal by another process after our stale read.
                current = read(task_id)
                current['state'] = 'sealed'
                current['frozen'] = {'payload_fingerprint': current['payload']['payload_fingerprint']}
                self.store._atomic_json(self.store._path(task_id), current)
            return snapshot

        self.store._read_existing = race
        try:
            with self.assertRaisesRegex(ChangeSetError, 'Concurrent ChangeSet modification'):
                self.store.set(self.task_id, fixture_payload(text='Overwrite sealed plan'))
        finally:
            self.store._read_existing = read
        self.assertEqual(self.store.get(self.task_id)['state'], 'sealed')
        self.assertEqual(self.store.get(self.task_id)['revision'], 1)
        self.assertEqual(self.store.get(self.task_id)['payload']['mutations'][0]['after']['content'], 'Original proposed text')

    def test_competing_writers_cannot_lose_revision(self):
        # Both callers read the same version before they contend for the lock.
        barrier = threading.Barrier(2)
        seen = threading.local()
        actual_read = self.store._read_existing
        def simultaneous_read(task_id):
            doc = actual_read(task_id)
            if not getattr(seen, 'read', False):
                seen.read = True
                barrier.wait(timeout=10)
            return doc
        self.store._read_existing = simultaneous_read
        def write(text):
            try:
                return self.store.set(self.task_id, fixture_payload(text=text))['revision']
            except ChangeSetError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(write, ('writer A', 'writer B')))
        self.assertEqual(sum(type(item) is int for item in outcomes), 1)
        self.assertTrue(any('Concurrent ChangeSet modification' in str(x) for x in outcomes))
        self.store._read_existing = actual_read
        self.assertEqual(self.store.get(self.task_id)['revision'], 1)


if __name__ == '__main__': unittest.main()
