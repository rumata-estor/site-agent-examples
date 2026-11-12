"""Browser-only synthetic fixtures for the actual public ChangeSetStore.

No CMS, Executor, human-approval provider or production connection exists.
The runtime component is imported unmodified by this module.
"""
import json
import shutil

from siteagent.changesets import ChangeSetStore, ChangeSetError
from siteagent.tasks import TaskError


def fixture_payload(text, risk, action, include_sensitive=False):
    after = {'id': 1, 'content': text}
    if include_sensitive:
        after['api_key'] = 'FAKE-EXAMPLE-VALUE'
    before = {'id': 1, 'content': 'Исходный текст'}
    if action == 'delete':
        after = {'exists': False}
    return {
        'summary': 'Изменение содержимого условного чанка',
        'mutations': [{
            'id': 'change-example-content', 'target_index': 0,
            'action': action, 'kind': 'chunk', 'before': before,
            'after': after, 'fields': ['content'], 'dependencies': [],
            'rationale': 'Демонстрация планирования изменения',
            'risk': {'level': risk, 'reasons': ['Интерактивный пример']},
        }],
        'approval_required': risk in ('high', 'critical') or action == 'delete',
        'approval_reason': 'Требуется согласие человека' if risk in ('high', 'critical') or action == 'delete' else None,
        'backup': {'required': True, 'strategy': 'element_snapshot',
                   'scope': ['chunk#1'], 'reason': 'Восстановление исходного содержимого'},
        'verification': [{'id': 'check-content', 'type': 'cms_read', 'target': 'chunk#1',
                          'expectation': 'Прочитать запланированное содержимое', 'required': True}],
        'rollback': {'strategy': 'restore_snapshot', 'steps': ['Вернуть исходный текст'],
                     'reason': 'Откат после сбоя', 'automatic_on_failure': False},
    }


def new_demo():
    root = '/site-demo/change-state'
    shutil.rmtree(root, ignore_errors=True)
    cfg = {
        'paths': {'manager_root': root},
        'task_manager': {'enabled': True},
        'changesets': {'enabled': True},
        'demo': {
            'stale': False,
            'resolution': {
                'fingerprint': 'synthetic-resolution-v1',
                'inventory_fingerprint': 'synthetic-inventory-v1',
                'architecture_fingerprint': 'synthetic-architecture-v1',
                'spec_revision': 1, 'observed_site_revision': 1,
                'operation': 'change',
                'targets': [{
                    'index': 0, 'kind': 'chunk', 'status': 'resolved', 'action': 'update',
                    'resolved': {'id': 1, 'name': 'test_chunk', 'in': 1},
                }],
            },
            'decision': {
                'revision': 1, 'updated_at': '2026-01-01T00:00:00+00:00',
                'source': {'resolution_fingerprint': 'synthetic-resolution-v1'},
            },
        },
    }
    store = ChangeSetStore('synthetic-site', cfg)
    task_id = store.task_store.create('Проверка безопасного плана')['id']
    return store, task_id, cfg


change_store, change_task_id, change_config = new_demo()


def current_state():
    try:
        doc = change_store.get(change_task_id)
    except ChangeSetError:
        return {'state': None, 'status': 'not_created'}
    payload = doc['payload']
    mutations = payload.get('mutations') or []
    return {
        'state': doc.get('state'),
        'revision': doc.get('revision'),
        'risk': payload.get('risk_level'),
        'approval_required': payload.get('approval_required'),
        'approval_reason': payload.get('approval_reason'),
        'source_fresh': doc.get('source_fresh'),
        'ready_for_execution': doc.get('ready_for_execution'),
        'stale_reasons': doc.get('stale_reasons'),
        'mutation': mutations[0] if mutations else None,
        'read_set_count': len(payload.get('read_set') or []),
        'fingerprint': payload.get('payload_fingerprint'),
        'history': doc.get('history', [])[-6:],
    }


def demo_change(raw):
    global change_store, change_task_id, change_config
    data = json.loads(raw)
    operation = str(data.get('action') or '')
    text = str(data.get('text') or '')
    risk = str(data.get('risk') or 'low')
    shared = bool(data.get('shared'))
    status = 'ok'
    message = ''
    try:
        if operation == 'reset':
            change_store, change_task_id, change_config = new_demo()
            message = 'Сброшен только временный пример в браузере.'
        elif operation in ('prepare', 'amend', 'scope_mismatch', 'sensitive'):
            change_config['demo']['resolution']['targets'][0]['resolved']['in'] = 4 if shared else 1
            planned_action = 'delete' if operation == 'scope_mismatch' else 'update'
            payload = fixture_payload(text, risk, planned_action, include_sensitive=operation == 'sensitive')
            if operation == 'amend':
                result = change_store.amend(change_task_id, payload,
                    reason='Изменены требования к тексту', source='browser-demo')
                message = 'План обновлён через amend; теперь снова требуется фиксация.'
            else:
                result = change_store.set(change_task_id, payload, source='browser-demo')
                message = 'План подготовлен и валидирован. Данные условного сайта не изменены.'
            if result.get('payload', {}).get('approval_required'):
                message += ' Для этого риска или общего элемента требуется доверенное подтверждение.'
        elif operation == 'seal':
            change_store.seal(change_task_id, reason='Проверено на демонстрационном стенде')
            message = 'Низкорисковый план зафиксирован; исполнение в демо не предусмотрено.'
        elif operation == 'approve':
            change_store.approve(change_task_id, reason='Нажата кнопка в браузере', source='user')
            message = 'Подтверждено'
        elif operation == 'stale':
            change_config['demo']['stale'] = True
            message = 'Исходные данные помечены как устаревшие. Повторная фиксация должна быть отклонена.'
        elif operation == 'tamper':
            document = change_store._read_existing(change_task_id)
            if not document or document.get('state') != 'sealed':
                raise ChangeSetError('Сначала зафиксируйте низкорисковый план')
            document['payload']['mutations'][0]['after']['content'] = 'Подменённое значение'
            change_store._atomic_json(change_store._path(change_task_id), document)
            message = 'Изменён файл зафиксированного плана. Проверка отпечатка должна обнаружить подмену.'
        elif operation == 'status':
            message = 'Проверено текущее состояние.'
        else:
            raise ChangeSetError('Неизвестный сценарий')
    except (ChangeSetError, TaskError, ValueError, TypeError, OSError) as exc:
        status = 'rejected'
        message = str(exc)
    state = current_state()
    return json.dumps({'status': status, 'message': message, 'change': state}, ensure_ascii=False)
