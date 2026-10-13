"""iMessage's optional native adapter, loaded on demand."""
from agentchat import _native

__getattr__ = _native.imessage_getattr
__dir__ = _native.imessage_dir
__all__ = ["IMessage"]
