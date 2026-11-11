"""Credential lookup (the operating system's credential store via keyring) and secret redaction."""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import sys

SERVICE = "ReadmeMonitor"
KINDS = {
    "github": ("github_token", "READMEMONITOR_GITHUB_TOKEN"),
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
}


def get(kind: str) -> tuple[str | None, str]:
    """Return (value, source). The credential store first, then an environment variable."""
    user, env = KINDS[kind]
    try:
        import keyring

        value = keyring.get_password(SERVICE, user)
        if value:
            return value, "credential-manager"
    except Exception as e:  # no usable backend in this logon session
        logging.getLogger(__name__).debug("keyring unavailable: %s", type(e).__name__)
    value = os.environ.get(env)
    if value:
        return value, f"environment:{env}"
    return None, "missing"


def store_name() -> str:
    if sys.platform == "win32":
        return "Windows Credential Manager"
    if sys.platform == "darwin":
        return "the macOS Keychain"
    return "the Secret Service keyring"


def _require_secure_backend():
    """Never fall back to a plaintext file: only the operating system's own stores are accepted."""
    import keyring

    kr = keyring.get_keyring()
    chain = getattr(kr, "backends", None) or [kr]
    names = [f"{type(b).__module__}.{type(b).__name__}" for b in chain]
    if not names or any(n.startswith("keyrings.alt") or n.startswith("keyring.backends.fail") or n.startswith("keyring.backends.null")
                        for n in names):
        raise RuntimeError(f"no secure credential store is available ({', '.join(names) or 'none'}); "
                           f"unlock or set up {store_name()} and try again")


def store(kind: str, value: str):
    import keyring

    _require_secure_backend()
    keyring.set_password(SERVICE, KINDS[kind][0], value)


def remove(kind: str) -> bool:
    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(SERVICE, KINDS[kind][0])
        return True
    except PasswordDeleteError:
        return False


# ---- redaction ----------------------------------------------------------------

SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"),
    # Values starting with {, $, < or % are template placeholders or references, not secrets.
    re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)\b(\s*[:=]\s*)(['\"])(?![{$<%])[^'\"\s]{8,}\3"),
]

# Files never read or sent, whatever the allowlist says.
SENSITIVE_GLOBS = (
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.keystore", "*.jks", "id_rsa*", "id_ed25519*",
    "*secret*", "*credential*", "*.sqlite", "*.sqlite3", "*.db", "*.kdbx", ".npmrc", ".pypirc", "*.tfstate",
)


def is_sensitive_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatch(name, g) for g in SENSITIVE_GLOBS)


def redact(text: str) -> tuple[str, int]:
    count = 0
    for pat in SECRET_PATTERNS:
        def sub(m):
            nonlocal count
            count += 1
            if m.lastindex and m.lastindex >= 3:  # keep the key name for key=value pairs
                return f"{m[1]}{m[2]}{m[3]}[REDACTED]{m[3]}"
            return "[REDACTED]"

        text = pat.sub(sub, text)
    return text, count


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        clean, n = redact(msg)
        if n:
            record.msg, record.args = clean, None
        return True
