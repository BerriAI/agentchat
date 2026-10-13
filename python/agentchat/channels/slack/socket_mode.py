"""Slack SDK types and native Socket Mode lifecycle."""
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.response import SocketModeResponse

from agentchat._native import SocketMode

EVENT_DRAIN_TIMEOUT = 20
__all__ = ["EVENT_DRAIN_TIMEOUT", "SocketMode", "SocketModeClient", "SocketModeResponse"]
