"""
Main FastAPI application: universal symbolic memory hub.

The API owns three things and orchestrates them:

    Prolog      embedded in-process via pyswip — the queryable knowledge base
    PostgreSQL  the durable store for facts, rules, embeddings and the audit trail
    LLM         a separate service reached over HTTP, only used to answer prompts

On startup the Prolog KB is rebuilt from PostgreSQL, so the API container is
disposable: kill it, start it again, and the knowledge is intact.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.database import bootstrap_kb_from_db, close_db, init_db
from src.api.embeddings import close_embedding_service
from src.api.prolog.client import close_prolog_client, get_prolog_client
from src.api.routes import agent, decide, domains, health, llm, memory, overrides, tree
from src.llm.base import close_llm_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting symbolic memory hub...")
    await init_db()
    logger.info("Database ready")

    # Start the embedded Prolog interpreter before bootstrapping so the KB can
    # be reloaded into it.
    await get_prolog_client()
    try:
        loaded = await bootstrap_kb_from_db()
        logger.info("Prolog KB restored with %d facts", loaded)
    except Exception as error:
        # A cold KB is not fatal: the API must still start so facts can be
        # written and /health can explain the problem.
        logger.error("Could not bootstrap the Prolog KB from PostgreSQL: %s", error)

    yield

    logger.info("Shutting down...")
    await close_llm_provider()
    await close_embedding_service()
    await close_prolog_client()
    await close_db()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Universal Symbolic Memory Hub",
    description=(
        "Prolog knowledge base with PostgreSQL persistence, pgvector semantic "
        "search and an HTTP-bridged LLM."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(memory.router)
app.include_router(domains.router)
app.include_router(agent.router)
app.include_router(llm.router)
app.include_router(tree.router)
app.include_router(decide.router)
app.include_router(overrides.router)
app.include_router(health.router)


@app.get("/")
async def root():
    return {
        "name": "Universal Symbolic Memory Hub",
        "version": "2.0.0",
        "docs": "/docs",
        "health": "/health",
        "endpoints": {
            "memory": "/memory/fact, /memory/rule, /memory/search, /memory/query",
            "domains": "/memory/domain",
            "agent": "/agent/ask",
            "llm": "/llm/ask",
            "decision_trees": "/tree, /trees, /decide",
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
