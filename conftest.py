"""Project-wide pytest guard: nothing run under pytest may open the owner's fare database.

It lives at the project root (not in tests/) so it also covers scratch and probe tests kept
elsewhere in the tree, e.g. under .superpowers/. A test helper that reaches database.py
without first pointing database.DB_PATH at a tmp path would otherwise migrate and write
data/flights.db: get_connection() migrates on open.
"""
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _no_real_database(monkeypatch):
    """Refuse to open data/flights.db (or whatever AVELO_DB_PATH names) during a test.

    Yields the list of refused paths. A refusal also fails the test at teardown, because
    the code under test may swallow the error (run_scraper closes its run row on any exception).
    """
    import config
    import database

    protected = {(PROJECT_ROOT / "data" / "flights.db").resolve(), Path(config.DB_PATH).resolve()}
    refused: list[str] = []
    real_get_connection = database.get_connection

    def guarded(db_path=None):
        target = database._resolve(db_path)
        if Path(target).resolve() in protected:
            refused.append(str(target))
            raise RuntimeError(
                f"test tried to open the real database {target}; "
                "point database.DB_PATH at a tmp path (or pass conn/':memory:') first"
            )
        return real_get_connection(db_path)

    monkeypatch.setattr(database, "get_connection", guarded)
    yield refused
    if refused:
        pytest.fail(f"the real database was requested {len(refused)} time(s) during this test: {refused[0]}")
