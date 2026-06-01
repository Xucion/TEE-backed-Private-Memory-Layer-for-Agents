import json
import uuid
from datetime import datetime,timezone
from pathlib import Path

import numpy as np
from cryptography.fernet import Fernet
from langchain_community.embeddings import DashScopeEmbeddings

PROJECT_ROOT = Path(__file__).resolve().parents[1]

MEMORY_DIR = PROJECT_ROOT / "vault_data"

SCHEMA_VERSION = 1
ACTIVE_STATUS = "active"
SUPERSEDED_STATUS = "superseded"
EXPIRED_STATUS = "expired"
FORGOTTEN_STATUS = "forgotten"

EMBEDDING_MODEL = "text-embedding-v4"
_embeddings = DashScopeEmbeddings(model=EMBEDDING_MODEL)

def _memory_file_for_user(user_id: str) -> Path:
    # 输入用户标识；输出该用户加密记忆文件路径；作用是实现 per-user 文件隔离。
    return MEMORY_DIR / f"{user_id}.memories.enc"

def _normalize(vec: np.ndarray) -> np.ndarray:
    # 输入 numpy 向量；输出单位化向量；作用是为向量相似度计算做归一化。
    norm = np.linalg.norm(vec)
    return vec / max(norm, 1e-9)

def _normalize_key_part(value: object) -> str:
    # 输入任意键片段值；输出小写下划线文本；作用是构造稳定 fact/conflict key。
    text = str(value or "").strip().lower()
    return "_".join(text.split())

def _load_memories(user_id: str, user_key: bytes) -> list[dict]:
    # 输入用户标识和 Fernet key；输出解密后的记忆列表；作用是读取该用户的加密记忆文件。
    memory_file = _memory_file_for_user(user_id)
    if not memory_file.exists():
        return []

    fernet = Fernet(user_key)
    with open(memory_file, "rb") as f:
        decrypted = fernet.decrypt(f.read())
    return json.loads(decrypted)


def _save_memories(user_id: str, user_key: bytes, memories: list[dict]) -> None:
    # 输入用户标识、Fernet key 和记忆列表；输出无返回值；作用是加密写回该用户记忆文件。
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    fernet = Fernet(user_key)
    encrypted = fernet.encrypt(json.dumps(memories, ensure_ascii=False).encode())
    with open(_memory_file_for_user(user_id), "wb") as f:
        f.write(encrypted)

def _find_embedding_duplicate(new_vec: np.ndarray, existing: list[dict], record: dict, threshold: float = 0.9,) -> dict | None:
    # 输入新向量、已有记忆和新记录；输出重复记忆或 None；作用是在同类槽位内查找语义重复。
    active_memories = [
        m for m in existing
        if m.get("status", ACTIVE_STATUS) == ACTIVE_STATUS
        and isinstance(m.get("embedding"), list)
        and m.get("memory_type") == record.get("memory_type")
        and m.get("predicate") == record.get("predicate")
        and m.get("slot") == record.get("slot")
    ]

    if not active_memories:
        return None

    existing_vecs = np.stack([
        np.array(m["embedding"])
        for m in active_memories
    ])

    scores = (existing_vecs @ new_vec).flatten()
    best_index = int(np.argmax(scores))
    best_score = float(scores[best_index])

    print(f"[MemoryStore] duplicate max_score={best_score:.3f}")

    if best_score < threshold:
        return None

    return active_memories[best_index]

def _parse_datetime(value: object) -> datetime | None:
    # 输入任意时间值；输出 UTC datetime 或 None；作用是解析 expires_at 等 ISO 时间字段。
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def _is_expired(memory: dict, now: datetime | None = None) -> bool:
    # 输入记忆和可选当前时间；输出是否过期；作用是判断 active 记忆是否超过 expires_at。
    expires_at = _parse_datetime(memory.get("expires_at"))
    if expires_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return expires_at <= now

def _apply_expiration(memories: list[dict]) -> tuple[list[dict], bool]:
    # 输入记忆列表；输出更新后的列表和是否变化；作用是把已过期 active 记忆标记为 expired。
    now = datetime.now(timezone.utc)
    changed = False

    for memory in memories:
        if memory.get("status", ACTIVE_STATUS) != ACTIVE_STATUS:
            continue

        if _is_expired(memory, now):
            memory["status"] = EXPIRED_STATUS
            memory["updated_at"] = now.isoformat()
            memory["forgotten_reason"] = "expired"
            changed = True

    return memories, changed

def _build_fact_key(mem: dict) -> str:
    # 输入记忆记录；输出稳定事实键；作用是识别可合并的同一事实。
    memory_type = _normalize_key_part(mem.get("memory_type", "other"))
    subject = _normalize_key_part(mem.get("subject", "user"))
    predicate = _normalize_key_part(mem.get("predicate", "stated_fact"))
    object_value = _normalize_key_part(mem.get("object") or mem.get("content"))
    return f"{memory_type}:{subject}:{predicate}:{object_value}"

def _build_conflict_key(mem: dict) -> str:
    # 输入记忆记录；输出冲突键；作用是识别互斥槽位中的旧事实。
    subject = _normalize_key_part(mem.get("subject", "user"))
    slot = _normalize_key_part(mem.get("slot"))
    if slot:
        return f"{subject}:slot:{slot}"
    return _build_fact_key(mem)

def _find_active_by_fact_key(memories: list[dict], fact_key: str) -> dict | None:
    # 输入记忆列表和事实键；输出匹配 active 记忆或 None；作用是查找可合并的已有事实。
    for memory in memories:
        if memory.get("status", ACTIVE_STATUS) != ACTIVE_STATUS:
            continue
        if memory.get("fact_key") == fact_key:
            return memory
    return None

def _find_active_conflicts(memories: list[dict], record: dict) -> list[dict]:
    # 输入记忆列表和新记录；输出冲突 active 记忆列表；作用是查找同槽位但不同事实的旧记录。
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
    # 输入冲突记忆和新记录；输出无返回值；作用是将旧冲突记忆标记为 superseded。
    now = datetime.now().isoformat()
    incoming.setdefault("supersedes", [])

    for memory in conflicts:
        memory["status"] = SUPERSEDED_STATUS
        memory["updated_at"] = now
        memory["superseded_by"] = incoming["id"]

        incoming["supersedes"].append(memory["id"])

def list_memories(
    user_id: str,
    user_key: bytes,
    status: str | None = None,
) -> list[dict]:
    memories = load_all_memories(user_id, user_key)

    if status:
        memories = [
            memory for memory in memories
            if memory.get("status", ACTIVE_STATUS) == status
        ]

    return [
        {
            key: value
            for key, value in memory.items()
            if key != "embedding"
        }
        for memory in memories
    ]

def touch_memories(user_id: str, user_key: bytes, memory_ids: list[str]) -> None:
    # 输入用户标识、密钥和记忆 ID 列表；输出无返回值；作用是更新检索访问时间和计数。
    if not memory_ids:
        return
    target_ids = set(memory_ids)
    memories = _load_memories(user_id, user_key)
    now = datetime.now().isoformat()
    changed = False
    for memory in memories:
        if memory.get("id") not in target_ids:
            continue
        memory["last_accessed_at"] = now
        memory["access_count"] = int(memory.get("access_count", 0)) + 1
        changed = True
    if changed:
        _save_memories(user_id, user_key, memories)

def _merge_memory(existing: dict, incoming: dict) -> None:
    # 输入已有记忆和新记忆；输出无返回值；作用是合并证据次数、时间戳和置信度。
    now = datetime.now().isoformat()

    existing["evidence_count"] = int(existing.get("evidence_count", 1)) + 1
    existing["last_seen_at"] = now
    existing["updated_at"] = now

    old_confidence = float(existing.get("confidence", 0.8))
    incoming_confidence = float(incoming.get("confidence", 0.8))
    boosted = max(old_confidence, incoming_confidence) + 0.03
    existing["confidence"] = min(0.99, boosted)

def forget_memories(
    user_id: str,
    user_key: bytes,
    memory_ids: list[str],
    reason: str = "user_requested",
) -> int:
    if not memory_ids:
        return 0
    
    target_ids = set(memory_ids)
    memories = _load_memories(user_id, user_key)
    now = datetime.now().isoformat()
    changed = 0

    for memory in memories:
        if memory.get("id") not in target_ids:
            continue
        if memory.get("status", ACTIVE_STATUS) != ACTIVE_STATUS:
            continue
        memory["status"] = FORGOTTEN_STATUS
        memory["forgotten_at"] = now
        memory["forgotten_reason"] = reason
        memory["updated_at"] = now
        changed += 1

    if changed > 0:
        _save_memories(user_id, user_key, memories)
        
    return changed


def _build_memory_record(mem: dict, embedding: list[float]) -> dict:
    # 输入规范化记忆和 embedding；输出完整存储记录；作用是补齐 schema、生命周期和索引字段。
    now = datetime.now().isoformat()
    confidence = float(mem.get("confidence", 0.8))

    record = {
        "schema_version": SCHEMA_VERSION,
        "id": str(uuid.uuid4()),

        "content": mem["content"],
        "memory_type": mem.get("memory_type", "other"),
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
    # 输入用户标识、密钥和新记忆列表；输出变更数量；作用是加密存储并处理合并、冲突和去重。
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


        embedding_duplicate = _find_embedding_duplicate(vec, existing, record)
        if embedding_duplicate is not None:
            _merge_memory(embedding_duplicate, record)
            print(f"[MemoryStore] 已合并语义重复记忆: {mem['content']}")
            changed += 1
            continue

        existing.append(record)
        changed += 1

    if changed > 0:
        _save_memories(user_id, user_key, existing)
        print(f"[MemoryStore] 已为 {user_id} 写入 {changed} 条记忆变更")
    return changed

def load_all_memories(user_id: str, user_key: bytes) -> list[dict]:
    # 输入用户标识和密钥；输出该用户全部记忆；作用是读取记忆并自动应用过期状态更新。
    memories = _load_memories(user_id, user_key)
    memories, changed = _apply_expiration(memories)

    if changed:
        _save_memories(user_id, user_key, memories)

    return memories
