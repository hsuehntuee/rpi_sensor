import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from src.db.local_sqlite import LocalDatabase


@pytest.fixture
def database() -> LocalDatabase:
    database = LocalDatabase(":memory:")
    yield database
    database.close()
