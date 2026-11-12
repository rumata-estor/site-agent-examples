"""Minimal adapters for private Runtime modules excluded from this repository."""
from pathlib import Path
import sys
import types


def install_environment_stubs():
    permissions = types.ModuleType("siteagent.permissions")

    def ensure_shared_directory(path):
        path = Path(path)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    permissions.ensure_shared_directory = ensure_shared_directory
    permissions.apply_shared_file_mode = lambda path: path
    permissions.chmod_if_owned = lambda path, mode: False
    sys.modules.setdefault("siteagent.permissions", permissions)

    config = types.ModuleType("siteagent.config")
    config.load_site = lambda site_id: (_ for _ in ()).throw(
        RuntimeError("Private Runtime config is not part of these examples")
    )
    sys.modules.setdefault("siteagent.config", config)
