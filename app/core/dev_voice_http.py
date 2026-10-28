"""Bound dev request bodies before multipart parsing, including chunked uploads."""

from ipaddress import ip_address

from starlette.responses import JSONResponse

from app.core.config import Settings
from app.modules.voice_loop.dev_api import DevApiError

MAX_REQUEST_BYTES = 4 * 1024 * 1024 + 65536


class DevRequestGuard:
    def __init__(self, app, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/dev/"):
            return await self.app(scope, receive, send)

        async def reject(status: int, code: str, message: str):
            response = JSONResponse(
                {"error": {"code": code, "message": message, "retryable": False}},
                status_code=status,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)

        if not self.settings.is_development or not self.settings.dev_voice_enabled:
            return await reject(404, "NOT_FOUND", "Not found.")
        try:
            client = scope.get("client")
            is_local = client is not None and ip_address(client[0]).is_loopback
        except ValueError:
            is_local = False
        if not is_local:
            return await reject(403, "LOCAL_ONLY", "This demo accepts local connections only.")
        headers = dict(scope.get("headers", []))
        length = headers.get(b"content-length")
        if length is not None:
            try:
                size = int(length)
            except ValueError:
                return await reject(400, "INVALID_LENGTH", "Invalid request size.")
            if size < 0 or size > MAX_REQUEST_BYTES:
                return await reject(413, "BODY_TOO_LARGE", "Request exceeds the demo upload limit.")
        total = 0

        async def bounded_receive():
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > MAX_REQUEST_BYTES:
                raise DevApiError(413, "BODY_TOO_LARGE", "Request exceeds the demo upload limit.")
            return message

        async def safe_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self'; style-src 'self'; "
                        b"connect-src 'self' https://saigon-veilwake-ai-assistant.onrender.com "
                        b"https://*.onrender.com; "
                        b"media-src 'self' blob: "
                        b"https://saigon-veilwake-ai-assistant.onrender.com; "
                        b"worker-src 'self'; "
                        b"object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
                    ),
                ]
            await send(message)

        await self.app(scope, bounded_receive, safe_send)
