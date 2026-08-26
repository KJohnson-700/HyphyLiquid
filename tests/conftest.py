"""Suite-wide guard: tests must never write into the live logs/ directory.

A test that repoints a script's data paths but misses its LOG_PATH silently
appends to the real log -- pytest lines ended up interleaved with production
output in logs/l2_cascade_features.log, which makes "is this daemon erroring?"
unanswerable from the log. Redirecting the whole suite is more reliable than
expecting every test to remember.
"""
import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _redirect_logs(tmp_path_factory):
    log_dir = tmp_path_factory.mktemp("logs")
    os.environ["HYPHYLIQUID_LOG_DIR"] = str(log_dir)
    yield log_dir
    os.environ.pop("HYPHYLIQUID_LOG_DIR", None)
