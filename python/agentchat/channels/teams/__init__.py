"""Teams' optional native adapter, loaded on demand."""
from agentchat import _native

__getattr__ = _native.teams_getattr
__dir__ = _native.teams_dir
__all__ = ["Teams"]
