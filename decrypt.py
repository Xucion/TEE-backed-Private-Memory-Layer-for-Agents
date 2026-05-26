from trusted.memory_store import load_all_memories

memories = load_all_memories()
for i, mem in enumerate(memories):
    print(f"[{i+1}] {mem['content']} | 分类: {mem['category']} | 敏感度: {mem['sensitivity']}")
