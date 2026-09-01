import pytest

import main
from providers import gemini


@pytest.fixture(autouse=True)
def _reset_module_globals():
    yield
    gemini._client = None
    for attr in ("summarizer", "semaphore", "background_tasks"):
        if hasattr(main.app.state, attr):
            delattr(main.app.state, attr)


class _FakeTransactionCtx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self):
        self.fetchrow_return = None
        self.fetchrow_calls = []
        self.execute_calls = []
        self.fetch_return = []
        self.fetch_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return self.fetchrow_return

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return self.fetch_return

    def transaction(self):
        return _FakeTransactionCtx()


class _FakeAcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, conn: FakeConn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self._conn)


@pytest.fixture
def fake_conn():
    return FakeConn()


@pytest.fixture
def fake_pool(fake_conn):
    return FakePool(fake_conn)
