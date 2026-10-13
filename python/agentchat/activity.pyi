# Public compatibility signatures; update with the corresponding Rust binding API.
from collections.abc import Awaitable, Callable
from types import TracebackType

class WorkingStatus:
    def __init__(
        self,
        setter: Callable[[str], Awaitable[bool]],
        status: str,
        *,
        refresh_interval: float = ...,
    ) -> None: ...
    async def update(self, status: str) -> bool: ...
    async def __aenter__(self) -> WorkingStatus: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...
