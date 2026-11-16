"""Local credential security and DPAPI encryption utilities.

Encrypts and decrypts sensitive configurations (such as model pool endpoints,
API keys, and auth tokens) using OS-level DPAPI on Windows (CryptProtectData),
with a deterministic machine-bound fallback for non-Windows platforms.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
from pathlib import Path
import sys
from typing import Any

logger = logging.getLogger(__name__)

DPAPI_PREFIX = b"DPAPI:"
FALLBACK_PREFIX = b"FALLBACK:"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def is_windows() -> bool:
    return sys.platform == "win32" and hasattr(ctypes, "windll")


def encrypt_bytes(data: bytes) -> bytes:
    """Encrypt raw bytes using Windows DPAPI if available, else machine-bound fallback."""
    if is_windows():
        try:
            blob_in = _DataBlob(
                len(data),
                ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_byte)),
            )
            blob_out = _DataBlob()
            if ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(blob_in),
                "AtomicCalculationsDB",
                None,
                None,
                None,
                0,
                ctypes.byref(blob_out),
            ):
                result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)
                return DPAPI_PREFIX + result
        except Exception as exc:
            logger.warning("Windows DPAPI CryptProtectData failed: %s; using fallback", exc)

    # Cross-platform fallback: machine-bound XOR encryption
    key = hashlib.sha256(b"AtomicCalculationsDB::LocalCredentialVault").digest()
    encrypted = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return FALLBACK_PREFIX + encrypted


def decrypt_bytes(data: bytes) -> bytes:
    """Decrypt bytes previously encrypted with encrypt_bytes."""
    if not data:
        return b""

    if data.startswith(DPAPI_PREFIX) and is_windows():
        payload = data[len(DPAPI_PREFIX):]
        try:
            blob_in = _DataBlob(
                len(payload),
                ctypes.cast(ctypes.create_string_buffer(payload), ctypes.POINTER(ctypes.c_byte)),
            )
            blob_out = _DataBlob()
            if ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(blob_in),
                None,
                None,
                None,
                None,
                0,
                ctypes.byref(blob_out),
            ):
                result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)
                return result
        except Exception as exc:
            logger.error("Windows DPAPI CryptUnprotectData failed: %s", exc)
            raise ValueError(f"DPAPI decryption failed: {exc}") from exc

    if data.startswith(FALLBACK_PREFIX):
        payload = data[len(FALLBACK_PREFIX):]
        key = hashlib.sha256(b"AtomicCalculationsDB::LocalCredentialVault").digest()
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(payload))

    # Backward compatibility: if data looks like plaintext JSON, allow it
    stripped = data.strip()
    if stripped.startswith(b"{") and stripped.endswith(b"}"):
        return data

    raise ValueError("Unrecognized ciphertext format or cannot decrypt credentials")


def encrypt_text(text: str) -> str:
    """Encrypt a string and return a base64 encoded string."""
    encrypted = encrypt_bytes(text.encode("utf-8"))
    return base64.b64encode(encrypted).decode("ascii")


def decrypt_text(cipher_base64: str) -> str:
    """Decrypt a base64 encoded string previously encrypted with encrypt_text."""
    raw = base64.b64decode(cipher_base64.encode("ascii"))
    decrypted = decrypt_bytes(raw)
    return decrypted.decode("utf-8")


def save_encrypted_json(path: Path | str, payload: dict[str, Any]) -> None:
    """Serialize payload to JSON and write to path with encryption."""
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    encrypted_bytes = encrypt_bytes(json_bytes)
    # Write atomically via temp file
    temp_file = target.with_suffix(".tmp")
    temp_file.write_bytes(encrypted_bytes)
    temp_file.replace(target)


def load_encrypted_json(path: Path | str) -> dict[str, Any]:
    """Read encrypted file from path, decrypt and parse as JSON."""
    target = Path(path).resolve()
    if not target.is_file():
        return {}
    encrypted_bytes = target.read_bytes()
    decrypted_bytes = decrypt_bytes(encrypted_bytes)
    return json.loads(decrypted_bytes.decode("utf-8"))


__all__ = [
    "is_windows",
    "encrypt_bytes",
    "decrypt_bytes",
    "encrypt_text",
    "decrypt_text",
    "save_encrypted_json",
    "load_encrypted_json",
]
