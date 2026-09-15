"""
Kalshi WebSocket / trader authentication helpers.

Resolution order for credentials (first hit wins per field):
  1. Explicit caller overrides (CLI flags / function args)
  2. Process environment variables
  3. Config-dir env files under ~/.config/kalshi-multiplex-orderbook/
  4. Config-dir split files (api-key-id + private-key.pem)
  5. Legacy prod-only ./grimm.txt private key fallback

Config directory layout (preferred):
    ~/.config/kalshi-multiplex-orderbook/
      prod.env                 # KEY=value lines; safe to source
      demo.env
      prod.private-key.pem     # default private key path for prod.env
      demo.private-key.pem

Example prod.env:
    KALSHI_PROD_API_KEY_ID=...
    KALSHI_PROD_PRIVATE_KEY_FILE=prod.private-key.pem

Legacy env names still work inside the file:
    KALSHI_API_KEY_ID=...
    KALSHI_PRIVATE_KEY_FILE=/absolute/path/to/key.pem

Split-file alternative (if no .env):
    prod.api-key-id
    prod.private-key.pem
    demo.api-key-id
    demo.private-key.pem

The WebSocket handshake signs:
    timestamp + "GET" + "/trade-api/ws/v2"
"""

from __future__ import annotations

import base64
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


WS_PATH = "/trade-api/ws/v2"

# Preferred environment-specific auth variables.
PROD_API_KEY_ID_ENV = "KALSHI_PROD_API_KEY_ID"
PROD_PRIVATE_KEY_FILE_ENV = "KALSHI_PROD_PRIVATE_KEY_FILE"
DEMO_API_KEY_ID_ENV = "KALSHI_DEMO_API_KEY_ID"
DEMO_PRIVATE_KEY_FILE_ENV = "KALSHI_DEMO_PRIVATE_KEY_FILE"

# Backward-compatible generic / older prod names.
LEGACY_API_KEY_ID_ENV = "KALSHI_API_KEY_ID"
LEGACY_PRIVATE_KEY_FILE_ENV = "KALSHI_PRIVATE_KEY_FILE"

DEFAULT_CONFIG_DIRNAME = "kalshi-multiplex-orderbook"
DEFAULT_PRIVATE_KEY_FILE = "grimm.txt"

_ENV_LINE_RE = re.compile(
    r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$"
)


def default_config_dir() -> Path:
    """Return ~/.config/kalshi-multiplex-orderbook (XDG-aware)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / DEFAULT_CONFIG_DIRNAME
    return Path.home() / ".config" / DEFAULT_CONFIG_DIRNAME


def config_env_file(kalshi_env: str, *, config_dir: Path | None = None) -> Path:
    """Path to prod.env / demo.env under the config dir."""
    root = config_dir or default_config_dir()
    env = _normalize_kalshi_env(kalshi_env)
    return root / f"{env}.env"


def config_api_key_id_file(kalshi_env: str, *, config_dir: Path | None = None) -> Path:
    root = config_dir or default_config_dir()
    return root / f"{_normalize_kalshi_env(kalshi_env)}.api-key-id"


def config_private_key_file(kalshi_env: str, *, config_dir: Path | None = None) -> Path:
    root = config_dir or default_config_dir()
    return root / f"{_normalize_kalshi_env(kalshi_env)}.private-key.pem"


def _normalize_kalshi_env(kalshi_env: str) -> str:
    text = str(kalshi_env or "").strip().lower()
    if text not in {"demo", "prod"}:
        raise ValueError(f"kalshi_env must be 'demo' or 'prod', got {kalshi_env!r}")
    return text


def auth_env_candidates(kalshi_env: str) -> list[tuple[str, str]]:
    """Return auth env-var pairs in preferred order for demo or prod."""
    env = _normalize_kalshi_env(kalshi_env)
    if env == "demo":
        return [
            (DEMO_API_KEY_ID_ENV, DEMO_PRIVATE_KEY_FILE_ENV),
            (LEGACY_API_KEY_ID_ENV, LEGACY_PRIVATE_KEY_FILE_ENV),
        ]
    return [
        (PROD_API_KEY_ID_ENV, PROD_PRIVATE_KEY_FILE_ENV),
        (LEGACY_API_KEY_ID_ENV, LEGACY_PRIVATE_KEY_FILE_ENV),
    ]


def default_auth_env_names(kalshi_env: str) -> tuple[str, str]:
    return auth_env_candidates(kalshi_env)[0]


def key_id_hint(api_key_id: str) -> str:
    """Return a non-secret hint for logs without printing the full key id."""
    text = str(api_key_id or "")
    if len(text) <= 12:
        return "set"
    return f"{text[:8]}...{text[-4:]}"


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


def _strip_env_value(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    if text[0] not in {"'", '"'}:
        if " #" in text:
            text = text.split(" #", 1)[0].rstrip()
        elif "\t#" in text:
            text = text.split("\t#", 1)[0].rstrip()
        return text
    quote = text[0]
    body = text[1:]
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            out.append(body[i + 1])
            i += 2
            continue
        if ch == quote:
            break
        out.append(ch)
        i += 1
    return "".join(out)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=value env file (supports optional `export ` prefix)."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Cannot read auth env file {path}: {exc}") from exc

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE_RE.match(line)
        if not match:
            raise RuntimeError(
                f"Invalid line {lineno} in {path}: {raw_line!r} "
                "(expected KEY=value or export KEY=value)"
            )
        key, raw_val = match.group(1), match.group(2)
        values[key] = _strip_env_value(raw_val)
    return values


def load_config_env_file(
    kalshi_env: str,
    *,
    config_dir: Path | None = None,
) -> tuple[dict[str, str], Path | None]:
    """Load {prod,demo}.env if present. Returns (values, path_or_none)."""
    path = config_env_file(kalshi_env, config_dir=config_dir)
    if not path.is_file():
        return {}, None
    return parse_env_file(path), path


def _lookup_first(
    keys: Iterable[str],
    *maps: Mapping[str, str] | None,
) -> tuple[str | None, str | None]:
    """Return (value, source_key) for the first present non-empty key across maps."""
    for mapping in maps:
        if not mapping:
            continue
        for key in keys:
            val = mapping.get(key)
            if val is not None and str(val).strip():
                return str(val).strip(), key
    return None, None


def _resolve_private_key_path(
    raw: str | None,
    *,
    kalshi_env: str,
    config_dir: Path,
    env_file: Path | None,
) -> Path | None:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if path.is_absolute():
        return path
    candidates = []
    if env_file is not None:
        candidates.append(env_file.parent / path)
    candidates.append(config_dir / path)
    candidates.append(Path.cwd() / path)
    for cand in candidates:
        if cand.is_file():
            return cand.resolve()
    if env_file is not None:
        return (env_file.parent / path).resolve()
    return (config_dir / path).resolve()


@dataclass(frozen=True)
class ResolvedAuth:
    """Non-secret metadata + secret key id for a resolved Kalshi credential set."""

    kalshi_env: str
    api_key_id: str
    private_key_path: str
    api_key_source: str
    private_key_source: str
    config_dir: str

    @property
    def api_key_hint(self) -> str:
        return key_id_hint(self.api_key_id)

    def load_private_key(self) -> Any:
        return load_private_key(self.private_key_path)


def resolve_kalshi_auth(
    kalshi_env: str,
    *,
    api_key_id: str | None = None,
    private_key_file: str | None = None,
    api_key_id_env: str | None = None,
    private_key_file_env: str | None = None,
    environ: Mapping[str, str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
    allow_grimm_fallback: bool = True,
    required: bool = True,
) -> ResolvedAuth | None:
    """Resolve API key id + private key path for demo/prod.

    Never prints secret values. When required=False, returns None if incomplete.
    """
    env = _normalize_kalshi_env(kalshi_env)
    env_map: Mapping[str, str] = environ if environ is not None else os.environ
    root = Path(config_dir).expanduser() if config_dir else default_config_dir()

    file_values, env_file_path = load_config_env_file(env, config_dir=root)
    candidates = auth_env_candidates(env)
    preferred_key_env, preferred_pem_env = candidates[0]

    key_names = [api_key_id_env] if api_key_id_env else [k for k, _ in candidates]
    pem_names = (
        [private_key_file_env] if private_key_file_env else [p for _, p in candidates]
    )
    key_names = [k for k in key_names if k]
    pem_names = [p for p in pem_names if p]

    resolved_key = str(api_key_id).strip() if api_key_id else None
    key_source = "cli:api-key-id" if resolved_key else None

    if not resolved_key:
        val, src = _lookup_first(key_names, env_map)
        if val:
            resolved_key, key_source = val, f"env:{src}"
    if not resolved_key:
        val, src = _lookup_first(key_names, file_values)
        if val:
            label = env_file_path.name if env_file_path else "env"
            resolved_key, key_source = val, f"config-env:{label}:{src}"

    if not resolved_key:
        key_file = config_api_key_id_file(env, config_dir=root)
        if key_file.is_file():
            text = key_file.read_text(encoding="utf-8").strip()
            if "=" in text and not text.startswith("-----"):
                parsed = parse_env_file(key_file)
                val, src = _lookup_first(key_names, parsed)
                if val:
                    resolved_key, key_source = val, f"config-file:{key_file.name}:{src}"
                elif len(parsed) == 1:
                    only = next(iter(parsed.values())).strip()
                    if only:
                        resolved_key, key_source = only, f"config-file:{key_file.name}"
            elif text:
                resolved_key = text.splitlines()[0].strip()
                key_source = f"config-file:{key_file.name}"

    resolved_pem_raw = str(private_key_file).strip() if private_key_file else None
    pem_source = "cli:private-key-file" if resolved_pem_raw else None

    if not resolved_pem_raw:
        val, src = _lookup_first(pem_names, env_map)
        if val:
            resolved_pem_raw, pem_source = val, f"env:{src}"
    if not resolved_pem_raw:
        val, src = _lookup_first(pem_names, file_values)
        if val:
            label = env_file_path.name if env_file_path else "env"
            resolved_pem_raw, pem_source = val, f"config-env:{label}:{src}"

    resolved_pem_path = _resolve_private_key_path(
        resolved_pem_raw,
        kalshi_env=env,
        config_dir=root,
        env_file=env_file_path,
    )

    if resolved_pem_path is None or not resolved_pem_path.is_file():
        sibling = config_private_key_file(env, config_dir=root)
        if sibling.is_file():
            resolved_pem_path = sibling.resolve()
            if not pem_source:
                pem_source = f"config-file:{sibling.name}"
            elif resolved_pem_raw and not Path(str(resolved_pem_raw)).expanduser().is_file():
                pem_source = f"config-file:{sibling.name}"

    if (
        (resolved_pem_path is None or not resolved_pem_path.is_file())
        and allow_grimm_fallback
        and env == "prod"
    ):
        grimm = Path(DEFAULT_PRIVATE_KEY_FILE).expanduser()
        if grimm.is_file():
            resolved_pem_path = grimm.resolve()
            pem_source = "fallback:./grimm.txt"

    missing: list[str] = []
    if not resolved_key:
        missing.append(
            "API key id (env "
            + ", ".join(key_names)
            + f" or {config_env_file(env, config_dir=root)} or {config_api_key_id_file(env, config_dir=root).name})"
        )
    if resolved_pem_path is None or not resolved_pem_path.is_file():
        missing.append(
            "private key PEM ("
            + ", ".join(pem_names)
            + f", --private-key-file, or {config_private_key_file(env, config_dir=root)})"
        )

    if missing:
        if not required:
            return None
        cfg = str(root)
        raise RuntimeError(
            f"Missing Kalshi {env} auth. Looked in process env and {cfg}. "
            + " | ".join(missing)
            + f". Create {config_env_file(env, config_dir=root)} with "
            f"{preferred_key_env}=... and {preferred_pem_env}=... "
            f"(relative PEM paths resolve under {cfg})."
        )

    assert resolved_key is not None
    assert resolved_pem_path is not None
    return ResolvedAuth(
        kalshi_env=env,
        api_key_id=resolved_key,
        private_key_path=str(resolved_pem_path),
        api_key_source=key_source or preferred_key_env,
        private_key_source=pem_source or preferred_pem_env,
        config_dir=str(root),
    )


def load_auth_from_env(
    *,
    api_key_env: str = LEGACY_API_KEY_ID_ENV,
    private_key_env: str = LEGACY_PRIVATE_KEY_FILE_ENV,
    kalshi_env: str | None = None,
    config_dir: str | os.PathLike[str] | None = None,
) -> tuple[str, Any]:
    """Load API key id and private key.

    Backward compatible with the old env-only API. When kalshi_env is omitted,
    probes prod then demo config/env sources using the provided env var names
    as preferred keys.
    """
    if kalshi_env:
        auth = resolve_kalshi_auth(
            kalshi_env,
            api_key_id_env=api_key_env,
            private_key_file_env=private_key_env,
            config_dir=config_dir,
            required=True,
        )
        assert auth is not None
        return auth.api_key_id, auth.load_private_key()

    env_key = os.environ.get(api_key_env)
    env_pem = os.environ.get(private_key_env)
    if env_key and env_pem:
        return env_key, load_private_key(env_pem)

    for probe in ("prod", "demo"):
        auth = resolve_kalshi_auth(
            probe,
            api_key_id_env=api_key_env,
            private_key_file_env=private_key_env,
            config_dir=config_dir,
            required=False,
        )
        if auth is not None:
            return auth.api_key_id, auth.load_private_key()

    raise RuntimeError(
        f"Missing auth. Set {api_key_env} + {private_key_env}, or create "
        f"{default_config_dir()}/prod.env (or demo.env)."
    )


def ensure_config_dir_scaffold(config_dir: str | os.PathLike[str] | None = None) -> Path:
    """Create the config directory if needed (does not write secrets)."""
    root = Path(config_dir).expanduser() if config_dir else default_config_dir()
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root
