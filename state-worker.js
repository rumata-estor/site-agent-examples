/* Loads real public state_guard.py and runs it via Python/WebAssembly.
 * SyntheticCMS does the fingerprint comparison; State Guard stores markers.
 * POSIX file locks and true concurrency cannot run in this single browser worker.
 */
const PYODIDE_BASE = 'https://cdn.jsdelivr.net/pyodide/v0.29.1/full/';
let pyodide;
let initialized = false;

async function initialize() {
  importScripts(PYODIDE_BASE + 'pyodide.js');
  pyodide = await loadPyodide({ indexURL: PYODIDE_BASE });
  const sources = ['siteagent/tasks.py', 'siteagent/state_guard.py', 'state-demo.py'];
  pyodide.FS.mkdirTree('/site-demo/siteagent');
  for (const path of sources) {
    const response = await fetch('./' + path, { cache: 'no-store' });
    if (!response.ok) throw new Error(`Не удалось загрузить ${path}: HTTP ${response.status}`);
    pyodide.FS.writeFile('/site-demo/' + path, await response.text());
  }
  pyodide.runPython(`
import sys, types
from pathlib import Path
sys.path.insert(0, '/site-demo')

# Explicitly unsupported by WebAssembly; no actual flock happens here.
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
config.load_site = lambda site_id: (_ for _ in ()).throw(RuntimeError('No Runtime config'))
sys.modules['siteagent.config'] = config

exec(open('/site-demo/state-demo.py', encoding='utf-8').read(), globals())
`);
  initialized = true;
}

self.onmessage = async ({data}) => {
  const id = data.id;
  try {
    if (data.kind === 'init') {
      if (!initialized) await initialize();
      self.postMessage({id, kind: 'ready'});
    } else if (data.kind === 'action') {
      if (!initialized) throw new Error('Python ещё загружается');
      const response = pyodide.globals.get('demo_guard')(JSON.stringify(data.payload));
      self.postMessage({id, kind: 'result', result: JSON.parse(response)});
    } else {
      throw new Error('Неизвестная команда');
    }
  } catch (err) {
    self.postMessage({id, kind: 'error', error: String(err.message || err)});
  }
};
