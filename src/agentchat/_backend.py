"""Backend selection is fixed before resources are bound or work is admitted."""
import os
from typing import Literal

Backend = Literal["rust", "python"]


def resolve_backend(backend: Backend | None) -> Backend:
    selected = backend if backend is not None else os.getenv("AGENTCHAT_BACKEND", "rust")
    if selected not in ("rust", "python"):
        raise ValueError("backend must be 'rust' or 'python'")
    return selected
