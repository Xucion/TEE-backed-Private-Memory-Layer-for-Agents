from memory_extractor import _normalize_memories

data = {
    "memories": [
        {
            "content": "我喜欢喝粥呀",
            "memory_type": "preference",
            "sensitivity": "low",
            "subject": "user",
            "predicate": "likes",
            "object": "粥",
            "value": True,
            "confidence": 0.85,
            "source": "user"
        }
    ]
}

print(_normalize_memories(data))
