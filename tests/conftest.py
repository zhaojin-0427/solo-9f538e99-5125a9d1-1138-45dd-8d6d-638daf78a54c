import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Keep the module-level default app from writing into the project directory.
os.environ.setdefault(
    "DATABASE", os.path.join(tempfile.mkdtemp(prefix="inspection-test-"), "default.db")
)


@pytest.fixture(autouse=True)
def clear_judge_caches():
    """Prepared-rule caches persist between tests; start each test cold."""
    from app import judge

    judge._PLAN_CACHE.clear()
    judge._MASK_CACHE.clear()
    yield
    judge._PLAN_CACHE.clear()
    judge._MASK_CACHE.clear()
