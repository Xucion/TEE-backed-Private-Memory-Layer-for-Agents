import numpy as np
from trusted.memory_store import load_all_memories
from langchain_community.embeddings import DashScopeEmbeddings

_embeddings = DashScopeEmbeddings(model="text-embedding-v4")

def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vec)
    return vec / max(norm, 1e-9)

def retrieve(query: str, top_k: int = 3, threshold: float = 0.4) -> list[dict]:
    memories = load_all_memories()
    if not memories:
        return []

    # 直接用存储的向量，无需重新编码
    mem_vecs = np.stack([np.array(m["embedding"]) for m in memories])
    query_embedding = _embeddings.embed_query(query)  # 返回 list[float]
    query_vec = _normalize(np.array(query_embedding))
    
    scores = (mem_vecs @ query_vec).flatten()
    top_indices = np.argsort(scores)[::-1][:top_k]

    return [
        {
            **{k: v for k, v in memories[idx].items() if k != "embedding"},
            "score": float(scores[idx])
        }
        for idx in top_indices
        if scores[idx] >= threshold
    ]
