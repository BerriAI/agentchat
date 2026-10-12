try:
    from agentchat.channels.teams.adapter import Teams
except ModuleNotFoundError as exc:
    if exc.name and exc.name.startswith("microsoft_agents"):
        raise ImportError("Install Teams support with: pip install 'agentchat[teams]'") from exc
    raise

__all__ = ["Teams"]
