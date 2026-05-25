from memory_store import load_all_memories
import json

memories = load_all_memories()
print(json.dumps(memories, ensure_ascii=False, indent=2))