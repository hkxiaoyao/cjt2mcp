"""crypto 模块测试。

核心是用户提供的权威测试向量：畅捷通官方文档给出的
key `1234567890123456` + 指定密文 → 指定明文。这是全案解密实现的验证锚点，必须通过。
"""

import base64
import json
import os

import pytest

from app.crypto import (
    decrypt_chanjet_message,
    decrypt_field,
    encrypt_chanjet_message,
    encrypt_field,
)


# ─── 权威测试向量（畅捷通官方文档） ───
VECTOR_KEY = "1234567890123456"
VECTOR_CIPHERTEXT = (
    "E4M54v2CbwnbdG+quqWwgFGI5dgx3shx2gGZRiihvkQQLgbH12Y9/dJXO1/7H7QLL3H9"
    "fstismlYMLQrZxShEyknFJcLG96HbG4Cx/7gq4YMXgZJDI9Qvm1sH6H4arIHaPTSbHTk"
    "faYo7fo6Sc3lwBMOpJHi33Os5u7DobPmqkzkuyoRxbTD4mZaSYleDcYuouQTdma+rubH"
    "5PPzg0+R09XsEHWkgF6cc+Ylh2w0N6590eJDNdQvoI4m7eSiWQCJo5nN5zXj/2QeQcYw"
    "IfdpmQ=="
)


def test_decrypt_official_vector():
    """官方测试向量：解密后应得到指定的业务 JSON。"""
    plaintext = decrypt_chanjet_message(VECTOR_CIPHERTEXT, VECTOR_KEY)
    data = json.loads(plaintext)

    assert data["id"] == "dbe8970a-53a7-165c-7339-02c55bbddea5"
    assert data["appKey"] == "FQa4kEGD"
    assert data["appId"] == "34526534673"
    assert data["msgType"] == "notice"
    assert data["time"] == "1603698652093"
    assert data["bizContent"]["value"] == "测试"


def test_encrypt_decrypt_roundtrip():
    """加密再解密应还原（自建应用 16 字节秘钥示例）。"""
    secret = "fuxinqiche202607"  # 16 字节 = AES-128
    original = json.dumps({"msgType": "APP_TEST", "appKey": "0aMlbJaE"})
    cipher = encrypt_chanjet_message(original, secret)
    assert decrypt_chanjet_message(cipher, secret) == original


def test_illegal_key_length():
    with pytest.raises(ValueError, match="消息秘钥长度"):
        decrypt_chanjet_message("YWJj", "short")


def test_illegal_ciphertext_length():
    # 合法 16 字节秘钥，但密文非 16 倍数
    with pytest.raises(ValueError, match="密文长度非法"):
        decrypt_chanjet_message(base64.b64encode(b"abc").decode(), VECTOR_KEY)


# ─── AES-GCM 字段加密 ───

def test_field_encrypt_roundtrip():
    master_key = os.urandom(32)
    secret = "419A91F08A66851E094E0E049887C6A8"
    enc = encrypt_field(secret, master_key)
    assert enc != secret  # 已加密
    assert decrypt_field(enc, master_key) == secret


def test_field_encrypt_nonce_differs():
    """同一明文两次加密应得到不同密文（随机 nonce）。"""
    master_key = os.urandom(32)
    a = encrypt_field("same", master_key)
    b = encrypt_field("same", master_key)
    assert a != b
    assert decrypt_field(a, master_key) == decrypt_field(b, master_key) == "same"
