/* Executes public changesets.py, alongside actual TaskStore, in browser Python.
 * All external DecisionSet, Resolver and StaleState inputs are synthetic.
 * No live connections or authorization are provided.
 */
const PYODIDE_BASE = 'https://cdn.jsdelivr.net/pyodide/v0.29.1/full/';
let pyodide;
let ready = false;

async function load() {
  importScripts(PYODIDE_BASE + 'pyodide.js');
  pyodide = await loadPyodide({ indexURL: PYODIDE_BASE });
  const paths = ['siteagent/tasks.py', 'siteagent/changesets.py', 'changeset-demo.py'];
  for (const path of paths) {
    const res = await fetch('./' + path, { cache: 'no-store' });
    if (!res.ok) throw new Error(`Не удалось загрузить ${path}: HTTP ${res.status}`);
    const body = await res.text();
    const target = '/site-demo/' + path;
    pyodide.FS.mkdirTree(target.slice(0, target.lastIndexOf('/')));
    pyodide.FS.writeFile(target, body);
  }
  pyodide.runPython(`
import sys, types, runpy
from pathlib import Path
sys.path.insert(0, '/site-demo')

# Browser environment: single Python worker and virtual files, no POSIX locks.
fcntl = types.ModuleType('fcntl')
fcntl.LOCK_EX = 2
fcntl.LOCK_UN = 8
fcntl.flock = lambda fd, mode: None
sys.modules['fcntl'] = fcntl

permissions = types.ModuleType('siteagent.permissions')
def ensure_shared_directory(path):
    Path(path).mkdir(parents=True, exist_ok=True)
    return Path(path)
permissions.ensure_shared_directory = ensure_shared_directory
permissions.apply_shared_file_mode = lambda path: path
permissions.chmod_if_owned = lambda path, mode: False
sys.modules['siteagent.permissions'] = permissions
config = types.ModuleType('siteagent.config')
config.load_site = lambda site: (_ for _ in ()).throw(RuntimeError('No live Runtime config'))
sys.modules['siteagent.config'] = config

decisions = types.ModuleType('siteagent.decisions')
class DecisionError(RuntimeError): pass
class DecisionStore:
    def __init__(self, site_id, cfg): self.fixture = cfg['demo']['decision']
    def get(self, task_id): return self.fixture
    def status(self, task_id):
        return {'available': True, 'revision': self.fixture['revision'],
                'source_fresh': True, 'ready_for_changeset': True}
decisions.DecisionError = DecisionError
decisions.DecisionStore = DecisionStore
sys.modules['siteagent.decisions'] = decisions

resolver = types.ModuleType('siteagent.resolver')
class ResolverError(RuntimeError): pass
class Resolver:
    def __init__(self, site_id, cfg): self.fixture = cfg['demo']['resolution']
    def get(self, task_id): return self.fixture
resolver.ResolverError = ResolverError
resolver.Resolver = Resolver
sys.modules['siteagent.resolver'] = resolver

state_guard = types.ModuleType('siteagent.state_guard')
class StaleStateStore:
    def __init__(self, site_id, cfg): self.cfg = cfg
    def invalidates(self, task_id, document_time): return bool(self.cfg['demo']['stale'])
state_guard.StaleStateStore = StaleStateStore
sys.modules['siteagent.state_guard'] = state_guard

exec(open('/site-demo/changeset-demo.py', encoding='utf-8').read(), globals())
`);
  ready = true;
}

self.onmessage = async ({data}) => {
  const id = data.id;
  try {
    if (data.kind === 'init') {
      if (!ready) await load();
      self.postMessage({ id, kind: 'ready' });
    } else if (data.kind === 'action') {
      if (!ready) throw new Error('Python ещё загружается');
      const result = JSON.parse(pyodide.globals.get('demo_change')(JSON.stringify(data.payload)));
      self.postMessage({ id, kind: 'result', result });
    } else {
      throw new Error('Неизвестная команда');
    }
  } catch (e) {
    self.postMessage({ id, kind: 'error', error: String(e.message || e) });
  }
};
