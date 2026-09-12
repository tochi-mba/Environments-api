"""Replace resolved credentials in captured output before anything stores it.

Without this, one ``env`` in a command puts a live credential into a buffer that is then
handed to an LLM. Secrets can straddle read boundaries, so a tail that could be the start
of a secret is held back until the next chunk decides.
"""

from __future__ import annotations

from app.constants import REDACTION_TEMPLATE


class Redactor:
    """Streaming search-and-replace for a fixed set of secrets."""

    def __init__(self, secrets: dict[str, str]) -> None:
        """Map each secret to the service it belongs to (used in the replacement marker)."""
        pairs = sorted(secrets.items(), key=lambda item: len(item[0]), reverse=True)
        self._secrets = [
            (secret.encode(), REDACTION_TEMPLATE.format(service=service).encode())
            for secret, service in pairs
            if secret
        ]
        self._max_len = max((len(s) for s, _ in self._secrets), default=0)
        self._pending = b""

    @property
    def active(self) -> bool:
        """Whether there is anything to redact at all."""
        return bool(self._secrets)

    def feed(self, data: bytes) -> bytes:
        """Return ``data`` with secrets replaced, minus a tail that might continue one."""
        if not self._secrets:
            return data
        buf = self._pending + data
        for secret, replacement in self._secrets:
            buf = buf.replace(secret, replacement)
        keep = self._holdback(buf)
        self._pending = buf[len(buf) - keep :] if keep else b""
        return buf[: len(buf) - keep]

    def flush(self) -> bytes:
        """Release the held-back tail; call when the command ends."""
        pending, self._pending = self._pending, b""
        return pending

    def _holdback(self, buf: bytes) -> int:
        longest = 0
        limit = min(len(buf), self._max_len - 1)
        for length in range(limit, 0, -1):
            tail = buf[-length:]
            if any(secret.startswith(tail) for secret, _ in self._secrets):
                longest = length
                break
        return longest
