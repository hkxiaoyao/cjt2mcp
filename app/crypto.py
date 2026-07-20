"""加解密核心模块。

两类互不相关的能力：

1. 畅捷通消息解密 —— 平台推送的消息用 AES/ECB/PKCS5Padding 加密，
   秘钥为该租户的"消息秘钥"UTF-8 字节直接作为 AES key（不做 Base64 解码）。
   信封形如 {"encryptMsg": "<Base64(密文)>"}。

2. 租户凭据字段加密 —— appSecret / 消息秘钥 / certificate / token 等敏感字段
   落库前用平台级 MASTER_KEY 做 AES-GCM 加密，页面永不回显明文。

两者用途、密钥来源、算法都不同，切勿混用。
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


# ─────────────────────────── 畅捷通消息解密（AES/ECB/PKCS5Padding）───────────────────────────

def _pkcs5_unpad(data: bytes) -> bytes:
    """去除 PKCS5/PKCS7 填充。填充字节值等于填充长度。"""
    if not data:
        raise ValueError("待去填充的数据为空")
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 16:
        raise ValueError("非法的 PKCS5 填充长度")
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("PKCS5 填充校验失败")
    return data[:-pad_len]


def _pkcs5_pad(data: bytes) -> bytes:
    """PKCS5/PKCS7 填充到 16 字节块边界。"""
    pad_len = 16 - (len(data) % 16)
    return data + bytes([pad_len]) * pad_len


def decrypt_chanjet_message(encrypt_msg: str, msg_secret: str) -> str:
    """解密畅捷通推送的 encryptMsg 字段，返回明文 JSON 字符串。

    :param encrypt_msg: 信封中的 encryptMsg（Base64 编码的 AES 密文）
    :param msg_secret: 该租户的消息秘钥（如 16 字节 = AES-128）
    :raises ValueError: 秘钥长度非法、Base64 解析失败、填充校验失败等
    """
    key = msg_secret.encode("utf-8")
    if len(key) not in (16, 24, 32):
        raise ValueError(f"消息秘钥长度必须为 16/24/32 字节，实际 {len(key)}")

    ciphertext = base64.b64decode(encrypt_msg)
    if len(ciphertext) == 0 or len(ciphertext) % 16 != 0:
        raise ValueError("密文长度非法（须为 16 字节整数倍且非空）")

    cipher = Cipher(algorithms.AES(key), modes.ECB())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    plaintext = _pkcs5_unpad(padded)
    return plaintext.decode("utf-8")


def encrypt_chanjet_message(plaintext: str, msg_secret: str) -> str:
    """对称的加密函数，主要用于测试构造加密消息。返回 Base64 密文。"""
    key = msg_secret.encode("utf-8")
    if len(key) not in (16, 24, 32):
        raise ValueError(f"消息秘钥长度必须为 16/24/32 字节，实际 {len(key)}")

    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    padded = _pkcs5_pad(plaintext.encode("utf-8"))
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode("ascii")


# ─────────────────────────── 租户凭据字段加密（AES-GCM）───────────────────────────

_GCM_NONCE_BYTES = 12


def _load_master_key() -> bytes:
    """从环境变量加载 AES-GCM 主密钥（Base64 编码的 32 字节）。"""
    raw = os.environ.get("MASTER_KEY", "")
    if not raw:
        raise RuntimeError("未配置 MASTER_KEY 环境变量")
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise RuntimeError(f"MASTER_KEY 解码后须为 32 字节，实际 {len(key)}")
    return key


def encrypt_field(plaintext: str, master_key: bytes | None = None) -> str:
    """用 AES-GCM 加密敏感字段，返回 Base64(nonce + ciphertext + tag)。

    :param plaintext: 明文（如 appSecret、certificate、token）
    :param master_key: 显式主密钥；为 None 时从 MASTER_KEY 环境变量加载
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = master_key if master_key is not None else _load_master_key()
    nonce = os.urandom(_GCM_NONCE_BYTES)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt_field(token: str, master_key: bytes | None = None) -> str:
    """解密 encrypt_field 产生的密文，返回明文。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = master_key if master_key is not None else _load_master_key()
    blob = base64.b64decode(token)
    nonce, ct = blob[:_GCM_NONCE_BYTES], blob[_GCM_NONCE_BYTES:]
    aesgcm = AESGCM(key)
    pt = aesgcm.decrypt(nonce, ct, None)
    return pt.decode("utf-8")
