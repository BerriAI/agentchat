try:
    from agentchat.channels.imessage.adapter import IMessage
except ModuleNotFoundError as exc:
    if exc.name == "socketio":
        raise ImportError(
            "Install iMessage support with: pip install 'agentchat[imessage]'"
        ) from exc
    raise

__all__ = ["IMessage"]
