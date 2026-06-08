import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from untrusted.agent_runtime import AgentService, AgentServiceError
from interface.vault_api import VaultApiError, relay_vault_request

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 输入 FastAPI app；输出 lifespan 迭代器；作用是按应用生命周期启动和关闭 AgentService。
    service = AgentService(
        redis_url=os.getenv(
            "REDIS_URL",
            "redis://127.0.0.1:6379/0",
        ),
        model_name=os.getenv("TONGYI_MODEL", "qwen-turbo"),
        history_ttl_seconds=int(
            os.getenv("CHAT_HISTORY_TTL_SECONDS", "86400")
        ),
    )

    service.start()
    app.state.agent_service = service

    try:
        yield
    finally:
        service.close()


app = FastAPI(
    title="Confidential Agent Memory Vault API",
    lifespan=lifespan,
)


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    reply: str
    memory_context_used: bool

class HandshakeRequest(BaseModel):
    nonce: str = Field(min_length=1, max_length=128)
    client_pubkey: str = Field(min_length=1, max_length=128)


class ProvisionEnvelope(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    nonce: str = Field(min_length=1, max_length=128)
    ciphertext: str = Field(min_length=1, max_length=2 * 1024 * 1024)


@app.post("/vault/handshake")
def relay_handshake(payload: HandshakeRequest):
    # 输入客户端握手参数；输出 vault 握手响应；作用是透明转发而不派生或保存信道密钥。
    try:
        return relay_vault_request({
            "action": "handshake_start",
            **payload.model_dump(),
        })
    except VaultApiError as exc:
        raise HTTPException(status_code=502, detail="Vault handshake failed") from exc


@app.post("/vault/provision")
def relay_provision(payload: ProvisionEnvelope):
    # 输入客户端加密 envelope；输出 vault 加密响应；作用是转发 key provisioning 而不接触明文 key。
    try:
        return relay_vault_request({
            "action": "secure_provision_user_key",
            **payload.model_dump(),
        })
    except VaultApiError as exc:
        raise HTTPException(status_code=502, detail="Vault provisioning failed") from exc

def get_agent_service(request: Request) -> AgentService:
    # 输入 FastAPI request；输出 AgentService；作用是从应用状态获取已启动的业务服务。
    return request.app.state.agent_service


@app.get("/health")
def health(request: Request) -> dict[str, bool]:
    # 输入 FastAPI request；输出健康状态；作用是确认 AgentService 已由 lifespan 初始化。
    get_agent_service(request)
    return {"ok": True}


@app.post("/chat", response_model=ChatResponse)
def chat(
    payload: ChatRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    vault_capability: str | None = Header(
        default=None,
        alias="X-Vault-Capability",
        max_length=512,
    ),
) -> ChatResponse:
    # 输入聊天请求和 capability header；输出 Agent 回复；作用是用 capability 调用 vault 数据面。
    service = get_agent_service(request)

    try:
        result = service.generate_reply(
            capability=vault_capability,
            session_id=payload.session_id,
            user_message=payload.message,
        )
    except AgentServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if vault_capability:
        background_tasks.add_task(
            service.store_memory_background,
            vault_capability,
            payload.message,
        )

    return ChatResponse(**result)
