"""Compatibility exports for the native WhatsApp adapter."""
import aiohttp  # noqa: F401 -- public transport injection compatibility

from agentchat._native import WhatsApp as WhatsApp

GRAPH_ROOT = "https://graph.facebook.com"
