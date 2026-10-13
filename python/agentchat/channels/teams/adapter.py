"""Thin class and FFI entry point for the native Teams adapter."""
from agentchat._native import load_teams

_TeamsBase = load_teams()


class Teams(_TeamsBase):
    __init__ = _TeamsBase.initialize
