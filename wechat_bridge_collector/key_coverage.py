"""Validate the key ring against the current account's actual database files."""
from __future__ import annotations

import hashlib
import hmac
import re
import struct
from pathlib import Path
from typing import Any

MESSAGE_DATABASE = re.compile(r"(?:biz_)?message_\d+\.db")
PAGE_SIZE = 4096


def database_inventory(db_dir: str) -> list[str]:
    root = Path(db_dir)
    # iterdir deliberately propagates missing-directory/permission errors.
    messages = sorted(
        "message/" + path.name for path in (root / "message").iterdir()
        if MESSAGE_DATABASE.fullmatch(path.name) and path.is_file()
    )
    if not messages:
        raise ValueError("未发现微信消息数据库，无法确认消息读取完整性")
    return ["contact/contact.db", "session/session.db", *messages]


def verify_page_key(page: bytes, key: str) -> bool:
    if len(page) != PAGE_SIZE or not re.fullmatch(r"[0-9a-fA-F]{64}", key):
        return False
    salt = bytes(value ^ 0x3A for value in page[:16])
    mac_key = hashlib.pbkdf2_hmac("sha512", bytes.fromhex(key), salt, 2, dklen=32)
    expected = hmac.new(mac_key, page[16:-64] + struct.pack("<I", 1), hashlib.sha512).digest()
    return hmac.compare_digest(expected, page[-64:])


def check_key_coverage(db_dir: str, keys: dict[str, Any]) -> list[str]:
    """Only read page one; never decrypt message content or expose key bytes."""
    normalized = {name.replace("\\", "/"): value for name, value in keys.items()}
    inventory = database_inventory(db_dir)
    missing, invalid = [], []
    for name in inventory:
        path = Path(db_dir) / name
        if path.is_symlink():
            raise ValueError(f"微信数据库不能是符号链接：{name}")
        with path.open("rb") as handle:
            page = handle.read(PAGE_SIZE)
        entry = normalized.get(name)
        if not isinstance(entry, dict) or not isinstance(entry.get("enc_key"), str):
            missing.append(name)
        elif not verify_page_key(page, entry["enc_key"]):
            invalid.append(name)
    if missing or invalid:
        details = []
        if missing:
            details.append("缺少密钥：" + "、".join(missing))
        if invalid:
            details.append("密钥校验失败或数据库尚未写入完整页：" + "、".join(invalid))
        raise ValueError(
            "微信数据库覆盖不完整；" + "；".join(details)
            + "。请通过自动获取或导入更新密钥，再重新检测；重复检测旧密钥不会补齐新库。"
        )
    return inventory
