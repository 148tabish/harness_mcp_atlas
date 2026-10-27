"""Key provider: the Bedrock API key from BB_BEDROCK_API_KEY, read at call time."""
from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


class CredentialsError(RuntimeError):
    """Raised when the configured environment variable is missing or empty."""


@runtime_checkable
class KeyProvider(Protocol):
    def get(self) -> str: ...


class EnvKeyProvider:
    """Reads the key from the environment on every call, so it can be rotated live."""

    def __init__(self, env_var: str = "BB_BEDROCK_API_KEY"):
        self.env_var = env_var

    def get(self) -> str:
        v = os.environ.get(self.env_var, "").strip()
        if not v:
            raise CredentialsError(f"environment variable {self.env_var!r} is not set or empty")
        return v
