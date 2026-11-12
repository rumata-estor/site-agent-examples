"""Browser demo for real StateGuard hashing and persistent stale markers.

A *separate synthetic memory adapter* checks expected fingerprints before writes.
StaleStateStore itself only records/queries invalidation markers and does not
perform atomic compare-and-swap against an external CMS.
"""
import json
import shutil

from siteagent.state_guard import StaleStateStore, StateGuardError, canonical_hash, utc_now

DEMO_TASK = 'TASK-20261112-091100-aabb'


class SyntheticCMS:
    """In-memory object, not a MODX/other CMS driver."""
    def __init__(self, initial_text='Исходный текст страницы'):
        self.data = {'id': 1, 'content': initial_text}
        self.version = 1

    def snapshot(self):
        return {'value': dict(self.data), 'version': self.version,
                'fingerprint': canonical_hash(self.data)}

    def write_if_current(self, expected, value):
        current = canonical_hash(self.data)
        if expected != current:
            return False
        self.data['content'] = value
        self.version += 1
        return True


def make_demo(initial_text='Исходный текст страницы'):
    root = '/site-demo/state-example'
    shutil.rmtree(root, ignore_errors=True)
    guard = StaleStateStore('synthetic-site', {'paths': {'manager_root': root}})
    return guard, SyntheticCMS(initial_text), {'A': None, 'B': None}, {'A': None, 'B': None}


guard, cms, observations, read_times = make_demo()


def view():
    marker = guard.get(DEMO_TASK)
    current = cms.snapshot()
    agents = {}
    for name in ('A', 'B'):
        saved = observations[name]
        agents[name] = {
            'fingerprint': saved,
            'has_read': saved is not None,
            'matches_current': saved == current['fingerprint'] if saved else None,
            'invalidated': guard.invalidates(DEMO_TASK, read_times[name]) if saved else None,
        }
    return {'object': current, 'agents': agents, 'marker': marker}


def demo_guard(raw):
    global guard, cms, observations, read_times
    params = json.loads(raw)
    action = params.get('action')
    status = 'ok'
    message = ''
    try:
        if action == 'reset':
            guard, cms, observations, read_times = make_demo(str(params.get('initial_text') or 'Исходный текст страницы'))
            message = 'Условный объект, отпечатки и маркеры сброшены.'
        elif action in ('read_a', 'read_b'):
            agent = action[-1].upper()
            read_times[agent] = utc_now()
            observations[agent] = cms.snapshot()['fingerprint']
            message = f'Агент {agent} прочитал текущую версию объекта.'
        elif action in ('write_a', 'write_b'):
            agent = action[-1].upper()
            expected = observations[agent]
            if expected is None:
                raise ValueError(f'Сначала нажмите «Агент {agent}: прочитать».')
            new_text = str(params.get('text_' + agent.lower()) or '').strip()
            if not new_text:
                raise ValueError('Текст для записи не может быть пустым')
            if len(new_text) > 4000:
                raise ValueError('Слишком длинный текст (максимум 4000 символов)')
            if not cms.write_if_current(expected, new_text):
                marker = guard.mark(DEMO_TASK, {
                    'reason': 'fingerprint_mismatch',
                    'agent': agent,
                    'expected': expected,
                    'observed': cms.snapshot()['fingerprint'],
                })
                status = 'rejected'
                message = f'STALE_STATE: запись агента {agent} остановлена. Объект изменился с момента чтения. Маркер №{marker["revision"]} записан.'
            else:
                message = f'Запись агента {agent} разрешена: ожидался актуальный отпечаток. Версия объекта: {cms.version}.'
        elif action == 'external':
            value = str(params.get('external_text') or '').strip()
            if not value:
                raise ValueError('Введите новый текст для внешнего изменения')
            if len(value) > 4000:
                raise ValueError('Слишком длинный текст (максимум 4000 символов)')
            cms.data['content'] = value
            cms.version += 1
            message = 'Смоделировано изменение объекта извне. Ранее считанные отпечатки больше не совпадают.'
        elif action == 'status':
            message = 'Проверено текущее состояние.'
        else:
            raise ValueError('Неизвестная операция')
    except (StateGuardError, ValueError, TypeError, OSError) as exc:
        status = 'rejected'
        message = str(exc)
    return json.dumps({'status': status, 'message': message, 'state': view()}, ensure_ascii=False)
