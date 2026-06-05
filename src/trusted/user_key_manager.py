import re
import secrets
import threading

from cryptography.fernet import Fernet


MAX_USER_ID_CHARS = 128
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_USER_KEYS: dict[str, bytes] = {}
_USER_KEYS_LOCK = threading.Lock()


class UserKeyError(Exception):
    """用户密钥注入或读取失败。"""


def _normalize_user_id(user_id: object) -> str:
    # 输入任意 user_id；输出规范化 user_id；作用是校验用户标识格式和长度。
    if not isinstance(user_id, str):
        raise UserKeyError("user_id 必须是字符串")

    normalized = user_id.strip()
    if not normalized:
        raise UserKeyError("user_id 不能为空")
    if len(normalized) > MAX_USER_ID_CHARS:
        raise UserKeyError(f"user_id 长度不能超过 {MAX_USER_ID_CHARS}")
    if not _USER_ID_RE.fullmatch(normalized):
        raise UserKeyError("user_id 只能包含字母、数字、下划线、点和短横线")

    return normalized


def _normalize_fernet_key(user_key: object) -> bytes:
    # 输入任意用户密钥；输出 Fernet key 字节串；作用是校验密钥类型和可用性。
    if isinstance(user_key, str):
        try:
            key = user_key.encode("ascii")
        except UnicodeEncodeError as exc:
            raise UserKeyError("user_key 必须是 ASCII Fernet key 字符串") from exc
    elif isinstance(user_key, bytes):
        key = user_key
    else:
        raise UserKeyError("user_key 必须是 Fernet key 字符串")

    try:
        Fernet(key)
    except (ValueError, TypeError) as exc:
        raise UserKeyError("user_key 不是有效 Fernet key") from exc

    return key


def provision_user_key(user_id: object, user_key: object) -> str:
    # 输入用户标识和 Fernet key；输出规范化 user_id；作用是把用户密钥注入进程内存表。
    normalized_user_id = _normalize_user_id(user_id)
    normalized_key = _normalize_fernet_key(user_key)

    with _USER_KEYS_LOCK:
        existing_key = _USER_KEYS.get(normalized_user_id)
        if existing_key is not None and not secrets.compare_digest(existing_key, normalized_key):
            raise UserKeyError("该 user_id 已绑定不同密钥，拒绝覆盖")
        _USER_KEYS[normalized_user_id] = normalized_key

    return normalized_user_id


def has_user_key(user_id: object) -> bool:
    # 输入用户标识；输出该用户是否已有 key；作用是查询进程内密钥表。
    normalized_user_id = _normalize_user_id(user_id)
    with _USER_KEYS_LOCK:
        return normalized_user_id in _USER_KEYS


def get_user_key(user_id: object) -> bytes:
    # 输入用户标识；输出 Fernet key 字节串；作用是从进程内密钥表读取用户密钥。
    normalized_user_id = _normalize_user_id(user_id)
    with _USER_KEYS_LOCK:
        key = _USER_KEYS.get(normalized_user_id)

    if key is None:
        raise UserKeyError("该 user_id 尚未注入密钥")
    return key
