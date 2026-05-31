import json
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
from cryptography.fernet import Fernet
from langchain_community.embeddings import DashScopeEmbeddings

PROJECT_ROOT = Path(__file__).resolve().parents[1]

MEMORY_DIR = PROJECT_ROOT / "vault_data"

SCHEMA_VERSION = 1
ACTIVE_STATUS = "active"
SUPERSEDED_STATUS = "superseded"

EMBEDDING_MODEL = "text-embedding-v4"
_embeddings = DashScopeEmbeddings(model=EMBEDDING_MODEL)

def _memory_file_for_user(user_id: str) -> Path:
    return MEMORY_DIR / f"{user_id}.memories.enc"

def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vec)
    return vec / max(norm, 1e-9)

def _normalize_key_part(value: object) -> str:
    text = str(value or "").strip().lower()
    return "_".join(text.split())

def _load_memories(user_id: str, user_key: bytes) -> list[dict]:
    memory_file = _memory_file_for_user(user_id)
    if not memory_file.exists():
        return []

    fernet = Fernet(user_key)
    with open(memory_file, "rb") as f:
        decrypted = fernet.decrypt(f.read())
    return json.loads(decrypted)


def _save_memories(user_id: str, user_key: bytes, memories: list[dict]) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    fernet = Fernet(user_key)
    encrypted = fernet.encrypt(json.dumps(memories, ensure_ascii=False).encode())
    with open(_memory_file_for_user(user_id), "wb") as f:
        f.write(encrypted)

def _is_duplicate(new_vec: np.ndarray, existing: list[dict], threshold: float = 0.9) -> bool:
    active_vecs = [
        np.array(m["embedding"])
        for m in existing
        if m.get("status", ACTIVE_STATUS) == ACTIVE_STATUS
        and isinstance(m.get("embedding"), list)
    ]

    if not active_vecs:
        return False

    existing_vecs = np.stack(active_vecs)
    scores = (existing_vecs @ new_vec).flatten()
    max_score = float(np.max(scores))
    print(f"[MemoryStore] duplicate max_score={max_score:.3f}")
    return float(np.max(scores)) >= threshold

def _build_fact_key(mem: dict) -> str:
    memory_type = _normalize_key_part(mem.get("memory_type", "other"))
    subject = _normalize_key_part(mem.get("subject", "user"))
    predicate = _normalize_key_part(mem.get("predicate", "stated_fact"))
    object_value = _normalize_key_part(mem.get("object") or mem.get("content"))
    return f"{memory_type}:{subject}:{predicate}:{object_value}"

def _build_conflict_key(mem: dict) -> str:
    subject = _normalize_key_part(mem.get("subject", "user"))
    slot = _normalize_key_part(mem.get("slot"))
    if slot:
        return f"{subject}:slot:{slot}"
    return _build_fact_key(mem)

def _find_active_by_fact_key(memories: list[dict], fact_key: str) -> dict | None:
    for memory in memories:
        if memory.get("status", ACTIVE_STATUS) != ACTIVE_STATUS:
            continue
        if memory.get("fact_key") == fact_key:
            return memory
    return None

def _find_active_conflicts(memories: list[dict], record: dict) -> list[dict]:
    conflicts = []
    for memory in memories:
        if memory.get("status", ACTIVE_STATUS) != ACTIVE_STATUS:
            continue
        if memory.get("conflict_key") != record.get("conflict_key"):
            continue
        if memory.get("fact_key") == record.get("fact_key"):
            continue
        conflicts.append(memory)
    return conflicts

def _supersede_conflicts(conflicts: list[dict], incoming: dict) -> None:
    now = datetime.now().isoformat()
    incoming.setdefault("supersedes", [])

    for memory in conflicts:
        memory["status"] = SUPERSEDED_STATUS
        memory["updated_at"] = now
        memory["superseded_by"] = incoming["id"]

        incoming["supersedes"].append(memory["id"])

def _merge_memory(existing: dict, incoming: dict) -> None:
    now = datetime.now().isoformat()

    existing["evidence_count"] = int(existing.get("evidence_count", 1)) + 1
    existing["last_seen_at"] = now
    existing["updated_at"] = now

    old_confidence = float(existing.get("confidence", 0.8))
    incoming_confidence = float(incoming.get("confidence", 0.8))
    boosted = max(old_confidence, incoming_confidence) + 0.03
    existing["confidence"] = min(0.99, boosted)

def _build_memory_record(mem: dict, embedding: list[float]) -> dict:
    now = datetime.now().isoformat()
    confidence = float(mem.get("confidence", 0.8))

    record = {
        "schema_version": SCHEMA_VERSION,
        "id": str(uuid.uuid4()),

        "content": mem["content"],
        "memory_type": mem.get("memory_type", "other"),
        "category": mem.get("category", "other"),
        "sensitivity": mem.get("sensitivity", "low"),

        "subject": mem.get("subject", "user"),
        "predicate": mem.get("predicate", "stated_fact"),
        "object": mem.get("object") or mem["content"],
        "value": mem.get("value", True),
        "slot": mem.get("slot"),

        "status": ACTIVE_STATUS,
        "confidence": min(1.0, max(0.0, confidence)),
        "evidence_count": 1,

        "source": mem.get("source", "user"),
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,
        "last_accessed_at": None,
        "access_count": 0,

        "expires_at": mem.get("expires_at"),
        "supersedes": [],
        "superseded_by": None,
        "forgotten_at": None,
        "forgotten_reason": None,

        "embedding": embedding,
        "embedding_model": EMBEDDING_MODEL,
    }

    record["fact_key"] = _build_fact_key(record)
    record["conflict_key"] = _build_conflict_key(record)
    return record

def store_memories(user_id: str, user_key: bytes, new_memories: list[dict]) -> int:
    if not new_memories:
        return 0
    existing = _load_memories(user_id, user_key)

    changed = 0

    for mem in new_memories:
        # 统一用 DashScope 编码
        vec = _normalize(np.array(_embeddings.embed_query(mem["content"])))
        record = _build_memory_record(mem, vec.tolist())

        existing_same_fact = _find_active_by_fact_key(existing, record["fact_key"])
        if existing_same_fact is not None:
            _merge_memory(existing_same_fact, record)
            changed += 1
            continue

        conflicts = _find_active_conflicts(existing, record)
        if conflicts:
            _supersede_conflicts(conflicts, record)
            existing.append(record)
            changed += 1
            continue


        if _is_duplicate(vec, existing):
            print(f"[MemoryStore] 跳过重复记忆: {mem['content']}")
            continue

        existing.append(record)
        changed += 1

    if changed > 0:
        _save_memories(user_id, user_key, existing)
        print(f"[MemoryStore] 已为 {user_id} 写入 {changed} 条记忆变更")
    return changed

def load_all_memories(user_id: str, user_key: bytes) -> list[dict]:
    return _load_memories(user_id, user_key)
