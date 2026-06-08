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
    # 输入 JSON 对象；输出稳定 UTF-8 字节串；作用是为签名和 HKDF transcript 固定编码。
    """为签名和 HKDF transcript 生成稳定字节串。"""
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def b64_encode(raw: bytes) -> str:
    # 输入原始字节；输出 base64 字符串；作用是把二进制字段放入 JSON 消息。
    """把字节数据编码为 base64 文本。"""
    return base64.b64encode(raw).decode("ascii")


def b64_decode(value: Any, field_name: str) -> bytes:
    # 输入待解码值和字段名；输出原始字节；作用是校验并解码 JSON 中的 base64 字段。
    """把 base64 文本解码为字节数据。"""
    if not isinstance(value, str):
        raise SecureChannelError(f"{field_name} 必须是 base64 字符串")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SecureChannelError(f"{field_name} 不是有效 base64") from exc


def generate_x25519_keypair():
    # 输入无显式参数；输出 X25519 私钥和公钥；作用是生成一次性密钥交换对。
    """生成 X25519 私钥和公钥对。"""
    private_key = x25519.X25519PrivateKey.generate()
    return private_key, private_key.public_key()


def public_key_to_b64(public_key) -> str:
    # 输入 X25519 公钥对象；输出 raw 公钥的 base64 字符串；作用是序列化公钥用于握手。
    """把 X25519 公钥序列化为 base64 文本。"""
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return b64_encode(raw)


def public_key_from_b64(value: Any, field_name: str):
    # 输入 base64 公钥和字段名；输出 X25519 公钥对象；作用是校验并反序列化握手公钥。
    """从 base64 文本还原 X25519 公钥。"""
    raw = b64_decode(value, field_name)
    if len(raw) != 32:
        raise SecureChannelError(f"{field_name} 长度无效")
    return x25519.X25519PublicKey.from_public_bytes(raw)


def derive_channel_key(
    shared_secret: bytes,
    quote: dict[str, Any],
    client_pubkey: str,
) -> bytes:
    # 输入共享秘密、quote 和客户端公钥；输出 32 字节信道密钥；作用是绑定 transcript 派生 AES key。
    # 绑定 quote 和 client public key，避免会话密钥脱离刚验证的身份声明。
    """派生安全信道、密钥数据。"""
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
    # 输入信道密钥、session_id 和明文对象；输出加密 envelope；作用是用 AES-GCM 加密 JSON payload。
    """加密JSON 数据。"""
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
    # 输入信道密钥、session_id、nonce 和密文；输出明文对象；作用是校验并解密 AES-GCM payload。
    """解密JSON 数据。"""
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
