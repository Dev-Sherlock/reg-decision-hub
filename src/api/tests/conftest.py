"""
Shared fixtures for the API tests.

The suite is async throughout. HTTP-level tests drive the app in-process over
ASGI rather than through TestClient, so everything shares one event loop — which
matters because the asyncpg pool and the embedded Prolog interpreter are both
bound to the loop that created them.

Isolation works in two halves:

* PostgreSQL — each test runs inside a transaction that is rolled back. Session
  commits become savepoint releases, so the helper functions under test can
  commit freely.
* Prolog — an in-memory store with no transactions, so it is cleared before and
  after every test instead.

Tests need a live stack. Anything that genuinely requires PostgreSQL,
SWI-Prolog or the embedding model skips with an explicit reason when that
dependency is missing, rather than failing with a confusing error.
"""
import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api import database as db
from src.api.embeddings import EmbeddingService, EmbeddingUnavailable, get_embedding_service
from src.api.main import app
from src.api.prolog.client import (
    MEMORY_MODULE,
    REGISTRY_MODULE,
    PrologClient,
    get_prolog_client,
)
from src.llm.base import LLMUnavailable, get_llm_provider

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/regdecision"),
)


# ---------------------------------------------------------------------------
# Core fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session", autouse=True, loop_scope="session")
async def stack():
    """Create the schema once for the whole session.

    The engine is swapped for a NullPool one first. asyncpg binds a connection
    to the event loop that opened it, and pytest gives every test its own loop,
    so a pooling engine eventually hands a test a connection belonging to a loop
    that has already finished. Opening a fresh connection per checkout makes the
    suite loop-agnostic without changing production behaviour.
    """
    db.engine = create_async_engine(
        TEST_DATABASE_URL, poolclass=NullPool, echo=False, pool_pre_ping=True
    )
    db.async_session_maker = async_sessionmaker(
        db.engine, class_=AsyncSession, expire_on_commit=False
    )

    try:
        async with db.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip(
            "PostgreSQL is not reachable. Start it with `docker compose up -d postgres`, "
            "or point DATABASE_URL/TEST_DATABASE_URL at a running instance."
        )

    try:
        await db.init_db()
        yield
    finally:
        await db.engine.dispose()


@pytest_asyncio.fixture
async def session(stack) -> AsyncIterator[AsyncSession]:
    """A session whose writes are discarded when the test ends."""
    connection = await db.engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()


@pytest_asyncio.fixture
async def prolog(stack) -> PrologClient:
    """The embedded Prolog client with an empty knowledge base."""
    client = await get_prolog_client()
    await _clear_kb(client)
    try:
        yield client
    finally:
        await _clear_kb(client)


async def _clear_kb(client: PrologClient) -> None:
    for predicate in ("clear_facts", "clear_rules", "clear_provenance"):
        await client.query(f"{MEMORY_MODULE}:{predicate}")
    # Runtime decision domains too. Prolog has no transactions to roll back with, so
    # a domain registered by one test would otherwise stay routable for the rest of
    # the session and quietly change what /decide does elsewhere.
    await client.query(f"{REGISTRY_MODULE}:clear_runtime_domains")


@pytest_asyncio.fixture
async def embeddings() -> EmbeddingService:
    return get_embedding_service()


@pytest_asyncio.fixture
async def embeddings_ready() -> EmbeddingService:
    """The embedding service, or a skip when the model cannot be loaded."""
    service = get_embedding_service()
    try:
        await service.load()
    except EmbeddingUnavailable as error:
        pytest.skip(f"sentence-transformers is unavailable: {error}")
    return service


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _in_process_client() -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield an in-process HTTP client."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest_asyncio.fixture
async def client(stack, session, prolog) -> AsyncIterator[httpx.AsyncClient]:
    """Full stack over HTTP, with the app writing through the test transaction."""
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[db.get_db] = override_get_db
    try:
        async with _in_process_client() as http:
            yield http
    finally:
        app.dependency_overrides.clear()


class _DownLLM:
    """An LLM provider whose backend is always unavailable."""

    async def ask(self, prompt: str, system_prompt=None) -> str:
        raise LLMUnavailable("simulated outage")

    async def is_healthy(self) -> bool:
        return False

    async def close(self) -> None:
        return None


@pytest_asyncio.fixture
async def client_without_llm(client) -> AsyncIterator[httpx.AsyncClient]:
    """The full stack with the LLM down, for the degradation tests."""
    app.dependency_overrides[get_llm_provider] = lambda: _DownLLM()
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_llm_provider, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def unique() -> str:
    """A short unique token so tests never collide on a subject."""
    return uuid.uuid4().hex[:10]
