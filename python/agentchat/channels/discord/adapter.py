"""Thin class and FFI entry point for the native Discord adapter."""
from agentchat._native import load_discord

_DiscordBase = load_discord()


class Discord(_DiscordBase):
    __init__ = _DiscordBase.initialize
