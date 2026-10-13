import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestServer

from agentchat.channels.telegram import Telegram
from agentchat.channels.telegram import adapter as telegram_adapter
from agentchat.channels.telegram.adapter import SECRET_HEADER
from agentchat.channels.telegram.events import normalize
from agentchat.channels.telegram.transport import TelegramBotClient, TelegramFile
from tests.support.receipts import Receipt

TOKEN = "42:SECRET-token_abcdef"
SECRET = "hook_secret-1"
HOOK_URL = "https://example.test/webhooks/telegram"
GOOD = {SECRET_HEADER: SECRET, "Content-Type": "application/json"}
BOT = {"id": 99, "is_bot": True, "first_name": "Bot", "username": "agentchat_bot"}
ALICE = {"id": 30, "is_bot": False, "first_name": "Alice", "last_name": "Liddell"}
GROUP = {"id": -1001, "type": "supergroup", "title": "Team"}


def telegram_message(**overrides):
    message = {
        "message_id": 100,
        "date": 1700000000,
        "from": dict(ALICE),
        "chat": {"id": 30, "type": "private", "first_name": "Alice"},
        "text": "hello",
    }
    return {**message, **overrides}


def telegram_update(update_id=1, **overrides):
    return {"update_id": update_id, "message": telegram_message(**overrides)}


def mention(text, entity_text, kind="mention", **extra):
    before = text[: text.index(entity_text)]
    return {
        "type": kind,
        "offset": len(before.encode("utf-16-le")) // 2,
        "length": len(entity_text.encode("utf-16-le")) // 2,
        **extra,
    }


def group_update(text="@agentchat_bot hi", entities=None, update_id=1, **overrides):
    if entities is None:
        entities = [mention(text, "@agentchat_bot")] if "@agentchat_bot" in text else []
    return telegram_update(update_id, chat=dict(GROUP), text=text, entities=entities, **overrides)


def callback_update(data="yes", update_id=2, **overrides):
    keyboard = {"inline_keyboard": [[
        {"text": "Yes please", "callback_data": "yes"},
        {"text": "No", "callback_data": "no"},
    ]]}
    callback = {
        "id": "cb-1",
        "from": dict(ALICE),
        "chat_instance": "instance",
        "data": data,
        "message": {
            "message_id": 55,
            "date": 1700000000,
            "from": dict(BOT),
            "chat": {"id": 30, "type": "private", "first_name": "Alice"},
            "text": "Continue?",
            "reply_markup": keyboard,
        },
    }
    return {"update_id": update_id, "callback_query": {**callback, **overrides}}


def read(update):
    return normalize(update, bot_id=99, bot_username="agentchat_bot")


def body(update_id, **overrides):
    return json.dumps(telegram_update(update_id, **overrides))


async def until(predicate):
    for _ in range(600):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("Condition was not reached")


async def nothing(channel, message):
    pass


def fast_retries(monkeypatch):
    monkeypatch.setattr(telegram_adapter, "RETRY_BASE", 0.01)
    monkeypatch.setattr(telegram_adapter, "BUSY_DELAY", 0.02)


def served(api, **options):
    client = TelegramBotClient(TOKEN, api_root=api.root)
    return Telegram(bot_token=TOKEN, client=client, **{"shutdown_timeout": 1, **options})


def faked(client=None, **options):
    return Telegram(bot_token="99:fake", client=client or TelegramClient(), **options)


def hooked(client=None, **options):
    return faked(client, webhook_url=HOOK_URL, webhook_secret=SECRET, **options)


class TelegramClient:
    def __init__(self, receipt=None, *, batches=None):
        self.receipt = receipt if receipt is not None else Receipt(101)
        self.batches = list(batches or [])
        self.calls = []
        self.uploads = []
        self.started, self.stopped = asyncio.Event(), asyncio.Event()
        self.closed = False

    def method_calls(self, method):
        return [payload for name, payload in self.calls if name == method]

    async def call(self, method, payload=None, **options):
        self.calls.append((method, payload))
        if method == "getMe":
            return dict(BOT)
        if method == "getUpdates":
            if payload.get("timeout") == 0:
                return []
            if self.batches:
                return self.batches.pop(0)
            self.started.set()
            await self.stopped.wait()
            return []
        if method == "sendMessage":
            return {"message_id": self.receipt.get(method, payload)}
        return True

    async def upload(self, *, method, fields, file_field, filename, content):
        self.uploads.append((method, fields, file_field, filename, content))
        return {"message_id": 200 + len(self.uploads),
                "document": {"file_id": f"file-{len(self.uploads)}"}}

    async def download_file(self, file_id, *, max_bytes):
        self.calls.append(("download_file", {"file_id": file_id, "max_bytes": max_bytes}))
        return TelegramFile(f"documents/{file_id}", b"content")

    async def close(self):
        self.closed = True
        self.stopped.set()


def _ok(result):
    return web.json_response({"ok": True, "result": result})


def _error(code, description, retry_after=None):
    body = {"ok": False, "error_code": code, "description": description}
    if retry_after:
        body["parameters"] = {"retry_after": retry_after}
    return web.json_response(body, status=code)


class FakeBotAPI:
    def __init__(self, token):
        self.token = token
        self.bot = {**BOT, "id": int(token.split(":")[0])}
        self.updates = []
        self.next_id = 100
        self.calls = []
        self.offsets = []
        self.webhook = None
        self.files = {}
        self.failures = {}
        self.poll_started = asyncio.Event()
        self._changed = asyncio.Condition()
        self.server = None
        self.root = None

    async def __aenter__(self):
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self._handle)
        app.router.add_get("/file/bot{token}/{path:.+}", self._download)
        self.server = TestServer(app)
        await self.server.start_server()
        self.root = str(self.server.make_url("")).rstrip("/")
        return self

    async def __aexit__(self, *args):
        await self.server.close()

    async def push(self, update):
        update = {**update, "update_id": self.next_id}
        self.next_id += 1
        self.updates.append(update)
        async with self._changed:
            self._changed.notify_all()
        return update["update_id"]

    def method_calls(self, method):
        return [payload for name, payload in self.calls if name == method]

    async def _payload(self, request):
        if request.content_type == "multipart/form-data":
            fields = {}
            async for part in await request.multipart():
                if part.filename:
                    fields[part.name] = (part.filename, await part.read())
                else:
                    fields[part.name] = await part.text()
            return fields
        return await request.json() if request.can_read_body else {}

    async def _handle(self, request):
        method = request.match_info["method"]
        payload = await self._payload(request)
        self.calls.append((method, payload))
        if request.match_info["token"] != self.token:
            return _error(401, "Unauthorized")
        scripted = self.failures.get(method)
        if scripted:
            status, body = scripted.pop(0)
            if body is None:
                return web.Response(status=status, text="<html>bad gateway</html>")
            return web.json_response(body, status=status)
        if method == "getMe":
            return _ok(self.bot)
        if method == "deleteWebhook":
            self.webhook = None
            return _ok(True)
        if method == "setWebhook":
            self.webhook = payload
            return _ok(True)
        if method == "getUpdates":
            return await self._get_updates(payload)
        if method == "sendMessage":
            return _ok({"message_id": 500 + len(self.method_calls("sendMessage")),
                        "chat": {"id": payload["chat_id"]}, "text": payload["text"]})
        if method == "sendDocument":
            return _ok({"message_id": 600, "document": {"file_id": "uploaded-file"}})
        if method == "getFile":
            file = self.files.get(payload["file_id"])
            return _ok(file["info"]) if file else _error(400, "Bad Request: invalid file_id")
        if method in ("sendChatAction", "answerCallbackQuery"):
            return _ok(True)
        return _error(404, "Not Found")

    async def _get_updates(self, payload):
        if self.webhook is not None:
            return _error(409, "Conflict: can't use getUpdates method while webhook is active")
        offset = payload.get("offset")
        self.offsets.append(offset)
        if offset is not None:
            self.updates = [u for u in self.updates if u["update_id"] >= offset]
        limit = payload.get("limit", 100)
        self.poll_started.set()
        async with self._changed:
            try:
                await asyncio.wait_for(
                    self._changed.wait_for(lambda: bool(self.updates)),
                    timeout=payload.get("timeout", 0) or 0.001,
                )
            except TimeoutError:
                pass
        return _ok(self.updates[:limit])

    async def _download(self, request):
        if request.match_info["token"] != self.token:
            return web.Response(status=404)
        for file in self.files.values():
            if file["info"].get("file_path") == request.match_info["path"] and file["content"]:
                return web.Response(body=file["content"])
        return web.Response(status=404)
