"""Native Discord REST transport and configurable API endpoint."""
from agentchat._native import DiscordRESTClient

API_ROOT = "https://discord.com/api/v10"
__all__ = ["DiscordRESTClient"]
