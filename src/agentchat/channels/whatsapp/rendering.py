def text_payload(content: str, *, recipient: str, message_id: str) -> dict:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("WhatsApp replies require nonempty text")
    if len(content) > 4096:
        raise ValueError("WhatsApp replies must fit 4,096 characters; send separate replies")
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"body": content, "preview_url": False},
        "context": {"message_id": message_id},
    }
