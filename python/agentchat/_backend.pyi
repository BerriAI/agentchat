from typing import Literal

Backend = Literal["rust"]

def resolve_backend(backend: Backend | None = ...) -> Backend: ...
