try:
    from agentchat.channels.discord.adapter import Discord
except ModuleNotFoundError as exc:
    if exc.name == "discord":
        raise ImportError("Install Discord support with: pip install 'agentchat[discord]'") from exc
    raise

__all__ = ["Discord"]
