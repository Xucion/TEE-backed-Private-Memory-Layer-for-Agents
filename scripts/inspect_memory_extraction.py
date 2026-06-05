import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from untrusted.memory_extractor import _normalize_memories

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
