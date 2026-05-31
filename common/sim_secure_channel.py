import base64
import binascii
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class SecureChannelError(Exception):
    """模拟安全信道里的编码、派生或解密失败。"""


def canonical_json(data: dict[str, Any]) -> bytes:
    """为签名和 HKDF transcript 生成稳定字节串。"""
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def b64_encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64_decode(value: Any, field_name: str) -> bytes:
    if not isinstance(value, str):
        raise SecureChannelError(f"{field_name} 必须是 base64 字符串")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SecureChannelError(f"{field_name} 不是有效 base64") from exc


def generate_x25519_keypair():
    private_key = x25519.X25519PrivateKey.generate()
    return private_key, private_key.public_key()


def public_key_to_b64(public_key) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return b64_encode(raw)


def public_key_from_b64(value: Any, field_name: str):
    raw = b64_decode(value, field_name)
    if len(raw) != 32:
        raise SecureChannelError(f"{field_name} 长度无效")
    return x25519.X25519PublicKey.from_public_bytes(raw)


def derive_channel_key(
    shared_secret: bytes,
    quote: dict[str, Any],
    client_pubkey: str,
) -> bytes:
    # 绑定 quote 和 client public key，避免会话密钥脱离刚验证的身份声明。
    transcript = {
        "purpose": "sim-ra-channel-v1",
        "quote": quote,
        "client_pubkey": client_pubkey,
    }
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=canonical_json(transcript),
    ).derive(shared_secret)


def encrypt_json(channel_key: bytes, session_id: str, payload: dict[str, Any]) -> dict[str, str]:
    nonce = os.urandom(12)
    plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ciphertext = AESGCM(channel_key).encrypt(
        nonce,
        plaintext,
        session_id.encode("utf-8"),
    )
    return {
        "nonce": b64_encode(nonce),
        "ciphertext": b64_encode(ciphertext),
    }


def decrypt_json(
    channel_key: bytes,
    session_id: str,
    nonce_b64: Any,
    ciphertext_b64: Any,
) -> dict[str, Any]:
    try:
        plaintext = AESGCM(channel_key).decrypt(
            b64_decode(nonce_b64, "nonce"),
            b64_decode(ciphertext_b64, "ciphertext"),
            session_id.encode("utf-8"),
        )
        payload = json.loads(plaintext.decode("utf-8"))
    except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError, SecureChannelError) as exc:
        raise SecureChannelError("安全信道解密失败") from exc

    if not isinstance(payload, dict):
        raise SecureChannelError("安全信道明文必须是 JSON 对象")
    return payload
