import json
import os
import uuid
from datetime import datetime
import numpy as np
from cryptography.fernet import Fernet
from langchain_community.embeddings import DashScopeEmbeddings

MEMORY_FILE = "memories.enc"
KEY_FILE = "memory.key"



_embeddings = DashScopeEmbeddings(model="text-embedding-v4")


def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vec)
    return vec / max(norm, 1e-9)

def _get_or_create_key() -> bytes:
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            return f.read()
    key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as f:
        f.write(key)
    return key

def _load_memories() -> list[dict]:
    if not os.path.exists(MEMORY_FILE):
        return []
    fernet = Fernet(_get_or_create_key())
    with open(MEMORY_FILE, "rb") as f:
        decrypted = fernet.decrypt(f.read())
    return json.loads(decrypted)

def _save_memories(memories: list[dict]) -> None:
    fernet = Fernet(_get_or_create_key())
    encrypted = fernet.encrypt(json.dumps(memories, ensure_ascii=False).encode())
    with open(MEMORY_FILE, "wb") as f:
        f.write(encrypted)

def _is_duplicate(new_content: str, existing: list[dict], threshold: float = 0.8) -> bool:
    if not existing:
        return False
    existing_vecs = np.stack([np.array(m["embedding"]) for m in existing])
    new_vec = _normalize(np.array(_embeddings.embed_query(new_content)))
    scores = (existing_vecs @ new_vec).flatten()
    print(np.max(scores))
    return float(np.max(scores)) >= threshold



def store_memories(new_memories: list[dict]) -> None:
    if not new_memories:
        return
    existing = _load_memories()

    added = 0

    for mem in new_memories:
        # 统一用 DashScope 编码
        vec = _normalize(np.array(_embeddings.embed_query(mem["content"])))

        if _is_duplicate(mem["content"], existing):
            print(f"[MemoryStore] 跳过重复记忆: {mem['content']}")
            continue
    
        mem["id"] = str(uuid.uuid4())
        mem["created_at"] = datetime.now().isoformat()
        mem["embedding"] = vec.tolist()
        mem["embedding_model"] = "text-embedding-v4"  # 记录模型版本
        existing.append(mem)
        added += 1

    if added > 0:
        _save_memories(existing)
        print(f"[MemoryStore] 已存储 {added} 条记忆")

def load_all_memories() -> list[dict]:
    return _load_memories()