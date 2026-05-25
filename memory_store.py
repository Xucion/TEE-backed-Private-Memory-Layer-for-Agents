import json
import os
import uuid
from datetime import datetime
from cryptography.fernet import Fernet

MEMORY_FILE = "memories.enc"
KEY_FILE = "memory.key"

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
    key = _get_or_create_key()
    fernet = Fernet(key)
    with open(MEMORY_FILE, "rb") as f:
        encrypted = f.read()
    decrypted = fernet.decrypt(encrypted)
    return json.loads(decrypted)

def _save_memories(memories: list[dict]) -> None:
    key = _get_or_create_key()
    fernet = Fernet(key)
    encrypted = fernet.encrypt(json.dumps(memories, ensure_ascii=False).encode())
    with open(MEMORY_FILE, "wb") as f:
        f.write(encrypted)

def store_memories(new_memories: list[dict]) -> None:
    if not new_memories:
        return
    existing = _load_memories()
    for mem in new_memories:
        mem["id"] = str(uuid.uuid4())
        mem["created_at"] = datetime.now().isoformat()
        existing.append(mem)
    _save_memories(existing)
    print(f"[MemoryStore] 已存储 {len(new_memories)} 条记忆")

def load_all_memories() -> list[dict]:
    return _load_memories()