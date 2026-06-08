import numpy as np
from trusted.memory_store import ACTIVE_STATUS, load_all_memories, touch_memories
from langchain_community.embeddings import DashScopeEmbeddings

_embeddings = DashScopeEmbeddings(model="text-embedding-v4")


def _normalize(vec: np.ndarray) -> np.ndarray:
    # 输入 numpy 向量；输出单位化向量；作用是为余弦相似度计算做归一化。
    """规范化当前函数的核心逻辑。"""
    norm = np.linalg.norm(vec)
    return vec / max(norm, 1e-9)


def retrieve(user_id: str, user_key: bytes, query: str, top_k: int = 3, threshold: float = 0.4) -> list[dict]:
    # 输入用户标识、密钥、查询和检索参数；输出匹配记忆列表；作用是按向量相似度召回 active 记忆。
    """按 embedding 相似度检索 active 记忆。"""
    memories = [
        m for m in load_all_memories(user_id, user_key)
        if m.get("status", ACTIVE_STATUS) == ACTIVE_STATUS
        and isinstance(m.get("embedding"), list)
    ]
    if not memories:
        return []

    # 直接用存储的向量，无需重新编码
    mem_vecs = np.stack([np.array(m["embedding"]) for m in memories])
    query_embedding = _embeddings.embed_query(query)  # 返回 list[float]
    query_vec = _normalize(np.array(query_embedding))
    
    scores = (mem_vecs @ query_vec).flatten()
    top_indices = np.argsort(scores)[::-1][:top_k]

    results = [
        {
            **{k: v for k, v in memories[idx].items() if k != "embedding"},
            "score": float(scores[idx])
        }
        for idx in top_indices
        if scores[idx] >= threshold
    ]

    touch_memories(
        user_id,
        user_key,
        [str(memory["id"]) for memory in results if memory.get("id")]
    )

    return results
