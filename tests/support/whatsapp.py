def whatsapp_event(*, messages=None, phone_number_id="123"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "business",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": phone_number_id},
                            "contacts": [{"wa_id": "15551234567", "profile": {"name": "Alice"}}],
                            "messages": messages
                            if messages is not None
                            else [
                                {
                                    "id": "wamid.incoming",
                                    "from": "15551234567",
                                    "type": "text",
                                    "text": {"body": "hello"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


class WhatsAppSession:
    def __init__(self, receipt):
        self.receipt, self.closed = receipt, False
        self.status = 200

    def post(self, *args, **kwargs):
        self.value = self.receipt.get(*args, **kwargs)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return {"messages": [{"id": self.value}]}

    async def close(self):
        self.closed = True
