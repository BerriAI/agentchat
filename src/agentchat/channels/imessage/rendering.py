def text_payload(content: str) -> str:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("iMessage replies require nonempty text")
    return content
