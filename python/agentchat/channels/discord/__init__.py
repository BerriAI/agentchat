"""Discord's optional native adapter, loaded on demand."""
from agentchat import _native

__getattr__ = _native.discord_getattr
__dir__ = _native.discord_dir
__all__ = ["Discord"]
