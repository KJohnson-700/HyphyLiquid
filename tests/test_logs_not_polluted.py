"""The production log directory is off-limits to the test suite."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_log_dir_is_redirected():
    override = os.environ.get("HYPHYLIQUID_LOG_DIR")
    assert override, "conftest should redirect logs for the whole suite"
    assert REPO_ROOT / "logs" != Path(override)


def test_scripts_resolve_log_path_at_call_time():
    import sys
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import build_l2_cascade_features as m
    resolved = m._log_path()
    assert str(resolved).startswith(os.environ["HYPHYLIQUID_LOG_DIR"]), resolved
    assert (REPO_ROOT / "logs") not in resolved.parents
