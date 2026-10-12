def text_payload(content: str) -> str:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Discord replies require nonempty text")
    # Count UTF-16 units as well, so astral characters cannot exceed the platform limit.
    if len(content.encode("utf-16-le")) // 2 > 2000:
        raise ValueError("Discord replies must fit 2,000 characters; send separate replies")
    return content
