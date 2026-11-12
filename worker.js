/* Site Agent Task Manager: real Python source executed in a Pyodide worker.
   Filesystem lives only in the browser session; fcntl is deliberately disabled.
   The real multiprocessing/flock checks run in GitHub Actions. */
const PYODIDE_BASE = 'https://cdn.jsdelivr.net/pyodide/v0.29.1/full/';
let ready = false;
let pyodide;

async function initialize() {
  importScripts(PYODIDE_BASE + 'pyodide.js');
  pyodide = await loadPyodide({ indexURL: PYODIDE_BASE });
  const response = await fetch('./siteagent/tasks.py', { cache: 'no-store' });
  if (!response.ok) throw new Error('Не удалось загрузить исходный файл tasks.py: HTTP ' + response.status);
  const source = await response.text();
  pyodide.FS.mkdirTree('/site-demo/siteagent');
  pyodide.FS.writeFile('/site-demo/siteagent/tasks.py', source);
  pyodide.runPython(`
import sys, types, json
from pathlib import Path
sys.path.insert(0, '/site-demo')

# Browser/WebAssembly does not provide POSIX advisory locks or independent
# Python processes. Explicit stub; this demo does NOT test flock correctness.
fcntl = types.ModuleType('fcntl')
fcntl.LOCK_EX = 2
fcntl.LOCK_UN = 8
fcntl.flock = lambda fd, mode: None
sys.modules['fcntl'] = fcntl

permissions = types.ModuleType('siteagent.permissions')
def ensure_shared_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path
permissions.ensure_shared_directory = ensure_shared_directory
permissions.apply_shared_file_mode = lambda path: path
permissions.chmod_if_owned = lambda path, mode: False
sys.modules['siteagent.permissions'] = permissions
config = types.ModuleType('siteagent.config')
config.load_site = lambda site_id: (_ for _ in ()).throw(RuntimeError('Live Runtime config is unavailable'))
sys.modules['siteagent.config'] = config

from siteagent.tasks import TaskStore, TaskError, VALID_STATUSES

CFG = {'paths': {'manager_root': '/site-demo/sandbox'},
       'task_manager': {'enabled': True, 'checkpoint_keep': 20, 'completed_keep': 50}}
store = TaskStore('browser-example', CFG)

def demo_action(raw):
    global store
    data = json.loads(raw)
    op = data.get('action')
    task_id = data.get('task_id')
    status = 'ok'
    result = ''
    created_id = None
    try:
        if op == 'create':
            task = store.create(str(data.get('objective') or ''), source='browser-demo')
            created_id = task['id']
            task_id = created_id
            result = 'Задача создана и сохранена в виртуальной файловой системе.'
        elif op == 'transition':
            task = store.transition(str(task_id), str(data.get('new_status') or ''),
                                    reason=str(data.get('reason') or '') or None)
            result = 'Переход выполнен: ' + task['status']
        elif op == 'invalid':
            if not task_id:
                raise TaskError('Сначала создайте задачу')
            old = store.get(str(task_id))['status']
            task = store.transition(str(task_id), old)
            result = 'Переход: ' + task['status']
        elif op == 'checkpoint':
            task = store.checkpoint(str(task_id),
                                    note=str(data.get('note') or 'Контрольная точка'),
                                    done=str(data.get('done') or '') or None,
                                    next_step=str(data.get('next_step') or '') or None)
            result = 'Контрольная точка записана. Ревизия: ' + str(task['revision'])
        elif op == 'repair':
            if not store.list():
                raise TaskError('Сначала создайте задачу')
            store.active_path.unlink(missing_ok=True)
            restored = store.active_id()
            result = ('Указатель удалён. Восстановлен: ' + restored) if restored else ('Указатель удалён. Занятых задач нет.')
        elif op == 'reset':
            import shutil
            shutil.rmtree('/site-demo/sandbox', ignore_errors=True)
            store = TaskStore('browser-example', CFG)
            task_id = None
            result = 'Состояние демонстрации очищено.'
        elif op != 'status':
            raise TaskError('Неизвестная команда')
    except (TaskError, ValueError, TypeError, OSError) as exc:
        status = 'rejected'
        result = str(exc)

    try:
        tasks = store.list(limit=40)
        current = store.get(str(task_id)) if task_id else None
        if current is None and tasks:
            task_id = tasks[0]['id']
            current = store.get(task_id)
        active_id = store.active_id()
        response = {'status': status, 'message': result, 'selected_id': task_id,
                    'active_id': active_id, 'tasks': tasks, 'current': current}
    except (TaskError, OSError, ValueError) as exc:
        response = {'status': 'rejected', 'message': str(exc), 'selected_id': task_id,
                    'active_id': None, 'tasks': [], 'current': None}
    return json.dumps(response, ensure_ascii=False)
`);
  ready = true;
}

self.onmessage = async ({ data }) => {
  const id = data.id;
  try {
    if (data.kind === 'init') {
      if (!ready) await initialize();
      self.postMessage({ id, kind: 'ready', engine: 'Python / Pyodide', source: 'siteagent/tasks.py' });
      return;
    }
    if (!ready) throw new Error('Интерпретатор ещё загружается');
    if (data.kind !== 'action') throw new Error('Неизвестная команда');
    const jsonString = pyodide.globals.get('demo_action')(JSON.stringify(data.payload));
    const result = JSON.parse(jsonString);
    self.postMessage({ id, kind: 'result', result });
  } catch (error) {
    self.postMessage({ id, kind: 'error', error: String(error?.message || error) });
  }
};
