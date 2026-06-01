from cryptography.fernet import Fernet

from trusted.user_key_manager import provision_user_key
from trusted.memory_store import _save_memories
from trusted.vault_server import _handle_list_memories_data, _handle_forget_data

user_id = "handler_list_forget_user"
user_key = Fernet.generate_key()

provision_user_key(user_id, user_key.decode("ascii"))

_save_memories(user_id, user_key, [
    {
        "id": "m1",
        "content": "用户喜欢喝粥",
        "status": "active",
        "embedding": [1.0, 0.0],
    }
])

listed = _handle_list_memories_data({
    "user_id": user_id,
    "status": "active",
})
print("listed:", listed)
assert listed["memory_count"] == 1
assert listed["memories"][0]["id"] == "m1"
assert "embedding" not in listed["memories"][0]

forgotten = _handle_forget_data({
    "user_id": user_id,
    "memory_id": "m1",
})
print("forgotten:", forgotten)
assert forgotten["forgotten_count"] == 1

listed_after = _handle_list_memories_data({
    "user_id": user_id,
    "status": "active",
})
print("listed_after:", listed_after)
assert listed_after["memory_count"] == 0

print("vault handlers OK")
