from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import aiohttp

API_ROOT = "https://api.telegram.org"
REQUEST_TIMEOUT = 30
UPLOAD_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 60
DOWNLOAD_CHUNK = 64 * 1024


def _download_limit_error(max_bytes: int) -> ValueError:
    return ValueError(f"Telegram file exceeds the {max_bytes / (1024 * 1024):g} MB download limit")


class TelegramError(RuntimeError):
    def __init__(
        self,
        description: str,
        *,
        error_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(description)
        self.description = description
        self.error_code = error_code
        self.retry_after = retry_after

    @property
    def is_transient(self) -> bool:
        code = self.error_code
        return code is None or code == 429 or code >= 500


def _unwrap_result(method: str, status: int, body: object) -> Any:
    if isinstance(body, dict) and body.get("ok") is True and "result" in body:
        return body["result"]
    if not isinstance(body, dict):
        raise TelegramError(f"Telegram {method} failed (HTTP {status})", error_code=status)
    parameters = body.get("parameters")
    retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
    code = body.get("error_code")
    description = body.get("description")
    raise TelegramError(
        f"Telegram {method} failed: {description if isinstance(description, str) else status}",
        error_code=code if isinstance(code, int) else status,
        retry_after=retry_after if isinstance(retry_after, int) and retry_after > 0 else None,
    )


class TelegramBotClient:
    """One-attempt Bot API calls; messages never carry the token-bearing URL."""

    def __init__(self, token: str, *, api_root: str = API_ROOT) -> None:
        self._token = token
        self._api_root = api_root.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._closed = False

    def _open_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise TelegramError("Telegram client is closed")
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _post(self, method: str, *, request_timeout: float, **request: Any) -> Any:
        session = self._open_session()
        try:
            async with session.post(
                f"{self._api_root}/bot{self._token}/{method}",
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=request_timeout),
                **request,
            ) as response:
                status = response.status
                try:
                    body = await response.json(content_type=None)
                except ValueError:
                    body = None
        except (aiohttp.ClientError, TimeoutError):
            raise TelegramError(
                f"Telegram {method} request failed; delivery may be uncertain"
            ) from None
        return _unwrap_result(method, status, body)

    async def call(
        self, method: str, payload: dict | None = None, *, request_timeout: float = REQUEST_TIMEOUT,
    ) -> Any:
        return await self._post(method, request_timeout=request_timeout, json=payload or {})

    async def upload(
        self, *, method: str, fields: dict, file_field: str, filename: str, content: bytes,
    ) -> Any:
        form = aiohttp.FormData()
        for key, value in fields.items():
            form.add_field(key, json.dumps(value) if isinstance(value, dict | list) else str(value))
        form.add_field(
            file_field, content, filename=filename, content_type="application/octet-stream",
        )
        return await self._post(method, request_timeout=UPLOAD_TIMEOUT, data=form)

    async def download_file(self, file_id: str, *, max_bytes: int) -> tuple[str, bytes]:
        file = await self.call("getFile", {"file_id": file_id})
        file = file if isinstance(file, dict) else {}
        size, path = file.get("file_size"), file.get("file_path")
        if isinstance(size, int) and size > max_bytes:
            raise _download_limit_error(max_bytes)
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or ".." in path.split("/")
        ):
            raise ValueError("This Telegram file is unavailable. Send the attachment again.")
        session = self._open_session()
        try:
            async with session.get(
                f"{self._api_root}/file/bot{self._token}/{quote(path)}",
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT),
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"Telegram file download failed (HTTP {response.status})")
                chunks, received = [], 0
                async for chunk in response.content.iter_chunked(DOWNLOAD_CHUNK):
                    received += len(chunk)
                    if received > max_bytes:
                        raise _download_limit_error(max_bytes)
                    chunks.append(chunk)
        except (aiohttp.ClientError, TimeoutError):
            raise TelegramError("Telegram file download failed") from None
        return path, b"".join(chunks)

    @property
    def is_closed(self) -> bool:
        return self._closed and (self._session is None or self._session.closed)

    async def close(self) -> None:
        self._closed = True
        if self._session:
            await self._session.close()
