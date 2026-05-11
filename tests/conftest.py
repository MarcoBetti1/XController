from __future__ import annotations

from pathlib import Path
import sys


repo_root = Path(__file__).resolve().parents[1]
repo_parent = repo_root.parent
if str(repo_parent) not in sys.path:
    sys.path.insert(0, str(repo_parent))

loaded = sys.modules.get("XController")
loaded_file = Path(str(getattr(loaded, "__file__", "") or ""))
if loaded is not None and repo_root not in loaded_file.parents and loaded_file != repo_root / "__init__.py":
    for module_name in list(sys.modules):
        if module_name == "XController" or module_name.startswith("XController."):
            sys.modules.pop(module_name, None)
