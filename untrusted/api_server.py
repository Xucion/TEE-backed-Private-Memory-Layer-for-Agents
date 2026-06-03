import logging
import os

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

from untrusted.agent_runtime import (
    AgentRuntimeError,
    generate_reply,
    store_memory_background,
)


logging.basicConfig(
    level=os.getenv("API_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [ApiServer] %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Confidential Agent Memory Vault API")


class ChatRequest(BaseModel):
    user_id: str = Field(default="default_user", min_length=1, max_length=128)
    session_id: str = Field(default="default_session", min_length=1, max_length=128)
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    reply: str
    memory_context_used: bool


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, background_tasks: BackgroundTasks) -> ChatResponse:
    try:
        result = generate_reply(
            user_id=req.user_id,
            session_id=req.session_id,
            user_message=req.message,
        )
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    background_tasks.add_task(
        store_memory_background,
        req.user_id,
        req.message,
    )

    return ChatResponse(**result)