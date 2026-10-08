"""
LLM service — FastAPI wrapper around llama-cpp-python.

Runs in its own container. It knows nothing about the symbolic memory: the API
retrieves relevant facts and passes them in the prompt, so this service is just
a completion endpoint over a GGUF model.

Model loading is deliberately non-fatal. If MODEL_PATH is missing or the file
is corrupt, the server still starts and reports unhealthy, leaving every
/memory endpoint working — the LLM is optional, the memory is not.

Environment
    MODEL_PATH      path to the .gguf inside the mounted /models volume
    N_CTX           context window
    N_GPU_LAYERS    layers to offload to GPU (0 = CPU only)
    REPEAT_PENALTY  discourages the completion loops small models fall into
"""
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

MODEL_PATH = os.getenv("MODEL_PATH", "/models/SmolLM2-135M-Instruct-Q4_K_M.gguf")
N_CTX = int(os.getenv("N_CTX", "2048"))
N_GPU_LAYERS = int(os.getenv("N_GPU_LAYERS", "0"))
REPEAT_PENALTY = float(os.getenv("REPEAT_PENALTY", "1.15"))

# Cut-offs for the case where a model runs the prompt back at us anyway. A
# chat-tuned model given raw completion text treats the prompt as a document to
# continue, which is why ask() prefers the GGUF's own chat template.
ECHO_STOPS = ["\nKnown facts:", "\nQuestion:", "\nSystem:", "</s>"]

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "You are a reasoning agent with access to a symbolic memory. Use the "
    "provided facts to inform your response. When you learn new facts, state "
    "them clearly."
)

_llm: Optional[Any] = None
_load_error: Optional[str] = None


def load_model() -> Optional[Any]:
    """Load the GGUF model. Returns None (and records why) instead of raising."""
    global _llm, _load_error
    if _llm is not None:
        return _llm

    if not os.path.exists(MODEL_PATH):
        _load_error = f"model file not found at {MODEL_PATH}"
        logger.error(_load_error)
        return None

    try:
        from llama_cpp import Llama
    except Exception as error:  # pragma: no cover - build-time failure
        _load_error = f"llama-cpp-python is unavailable: {error}"
        logger.error(_load_error)
        return None

    try:
        logger.info("Loading model from %s...", MODEL_PATH)
        _llm = Llama(
            model_path=MODEL_PATH,
            n_ctx=N_CTX,
            n_gpu_layers=N_GPU_LAYERS,
            verbose=False,
            n_threads=max(1, (os.cpu_count() or 4) - 1),
        )
        _load_error = None
        logger.info("Model loaded successfully")
        return _llm
    except Exception as error:
        _load_error = f"failed to load {MODEL_PATH}: {error}"
        logger.error(_load_error)
        _llm = None
        return None


def _chat_capable(model: Any) -> bool:
    """True when the loaded GGUF carries a chat template llama.cpp can apply.

    Applying it is what stops a chat-tuned model from treating the prompt as a
    document to continue and answering, then re-asking the question in a loop.
    """
    try:
        if getattr(model, "chat_handler", None):
            return True
        return bool((getattr(model, "metadata", None) or {}).get("tokenizer.chat_template"))
    except Exception:  # pragma: no cover - defensive
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _llm
    load_model()
    yield
    _llm = None
    logger.info("Model unloaded")


app = FastAPI(title="LLM Service", lifespan=lifespan)


class AskRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    system: Optional[str] = None
    max_tokens: int = Field(400, ge=1, le=4096)
    temperature: float = Field(0.3, ge=0.0, le=2.0)


class AskResponse(BaseModel):
    response: str


@app.post("/ask", response_model=AskResponse)
async def ask(request: AskRequest):
    """Answer a prompt. Returns 503 when no model is loaded."""
    model = _llm if _llm is not None else load_model()
    if model is None:
        raise HTTPException(status_code=503, detail=_load_error or "model not loaded")

    system = request.system or DEFAULT_SYSTEM_PROMPT
    chat = _chat_capable(model)
    try:
        if chat:
            result = model.create_chat_completion(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": request.prompt},
                ],
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                top_p=0.9,
                repeat_penalty=REPEAT_PENALTY,
                stop=ECHO_STOPS,
            )
            text = (result["choices"][0]["message"]["content"] or "").strip()
        else:
            result = model(
                f"{system}\n\n{request.prompt}",
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                top_p=0.9,
                repeat_penalty=REPEAT_PENALTY,
                stop=ECHO_STOPS,
                echo=False,
            )
            text = (result["choices"][0]["text"] or "").strip()
    except Exception as error:
        logger.error("Inference failed: %s", error)
        raise HTTPException(status_code=500, detail=f"inference failed: {error}")

    if not text:
        raise HTTPException(status_code=502, detail="model returned an empty completion")
    return AskResponse(response=text)


@app.get("/health")
async def health():
    """Health check. Always 200 so the container stays up; status says why."""
    if _llm is None:
        return {"status": "unhealthy", "model": MODEL_PATH, "reason": _load_error or "model not loaded"}
    return {
        "status": "healthy",
        "model": MODEL_PATH,
        "mode": "chat" if _chat_capable(_llm) else "completion",
    }
