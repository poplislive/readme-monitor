"""Credential lookup (Windows Credential Manager via keyring) and secret redaction."""

from __future__ import annotations

import fnmatch
import logging
import os
import re

SERVICE = "ReadmeMonitor"
KINDS = {
    "github": ("github_token", "READMEMONITOR_GITHUB_TOKEN"),
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
}


def get(kind: str) -> tuple[str | None, str]:
    """Return (value, source). Credential Manager first, then an environment variable."""
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


def store(kind: str, value: str):
    import keyring

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
