"""The implementation is native Rust; no interpreter fallback exists."""
from typing import Literal

from agentchat._native import resolve_backend

Backend = Literal["rust"]
__all__ = ["Backend", "resolve_backend"]
