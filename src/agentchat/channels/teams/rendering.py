def text_payload(content: str) -> str:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Teams replies require nonempty text")
    # Leave space for the activity envelope below Teams' 40 KB message limit.
    if len(content.encode("utf-8")) > 24_000:
        raise ValueError("Teams text replies must fit 24,000 UTF-8 bytes; send separate replies")
    return content
