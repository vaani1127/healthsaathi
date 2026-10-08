from starlette.types import ASGIApp, Message, Receive, Scope, Send

API_HEADERS = {
    b"strict-transport-security": b"max-age=63072000; includeSubDomains",
    b"content-security-policy": b"default-src 'none'; frame-ancestors 'none'",
    b"x-content-type-options": b"nosniff",
    b"referrer-policy": b"no-referrer",
    b"x-frame-options": b"DENY",
    b"cross-origin-opener-policy": b"same-origin",
    b"permissions-policy": b"camera=(), microphone=(), geolocation=()",
    b"cache-control": b"no-store",
}

# The interactive API docs load their own scripts and styles, so they get a looser policy.
DOCS_PREFIXES = ("/docs", "/redoc")
DOCS_CSP = (
    b"default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    b"style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    b"img-src 'self' data: https://fastapi.tiangolo.com; frame-ancestors 'none'"
)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_docs = scope["path"].startswith(DOCS_PREFIXES)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", [])}
                headers = list(message.get("headers", []))
                for name, value in API_HEADERS.items():
                    if name == b"content-security-policy" and is_docs:
                        value = DOCS_CSP
                    if name not in existing:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
