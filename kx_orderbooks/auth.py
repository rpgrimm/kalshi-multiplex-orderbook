"""
Kalshi WebSocket authentication helpers.

Expected environment variables by default:
    KALSHI_API_KEY_ID
    KALSHI_PRIVATE_KEY_FILE

The WebSocket handshake signs:
    timestamp + "GET" + "/trade-api/ws/v2"
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any, Dict

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


WS_PATH = "/trade-api/ws/v2"


def load_private_key(private_key_file: str | os.PathLike[str]) -> Any:
    """Load a PEM RSA private key from disk."""
    private_key_path = Path(private_key_file).expanduser()
    with private_key_path.open("rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign_pss_text(private_key: Any, text: str) -> str:
    """Sign text with RSA-PSS/SHA256 and return base64 signature text."""
    signature = private_key.sign(
        text.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def create_ws_headers(
    *,
    api_key_id: str,
    private_key: Any,
    method: str = "GET",
    path: str = WS_PATH,
) -> Dict[str, str]:
    """Create Kalshi auth headers for a WebSocket connection."""
    timestamp = str(int(time.time() * 1000))
    msg_string = timestamp + method + path.split("?", 1)[0]
    signature = sign_pss_text(private_key, msg_string)

    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }


def load_auth_from_env(
    *,
    api_key_env: str = "KALSHI_API_KEY_ID",
    private_key_env: str = "KALSHI_PRIVATE_KEY_FILE",
) -> tuple[str, Any]:
    """Load API key id and private key using environment variables."""
    try:
        api_key_id = os.environ[api_key_env]
    except KeyError as exc:
        raise RuntimeError(f"Missing env var {api_key_env}") from exc

    try:
        private_key_file = os.environ[private_key_env]
    except KeyError as exc:
        raise RuntimeError(f"Missing env var {private_key_env}") from exc

    return api_key_id, load_private_key(private_key_file)
