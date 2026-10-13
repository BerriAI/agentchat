"""Thin class and FFI entry point for the native IMessage adapter."""
from agentchat._native import load_imessage

_IMessageBase = load_imessage()


class IMessage(_IMessageBase):
    __init__ = _IMessageBase.initialize
