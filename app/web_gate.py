import hashlib
import hmac
import os
from http.cookies import SimpleCookie

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.config import SECRET_KEY


COOKIE_NAME = "blits_web_gate"


def normalize_web_path(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    value = "/" + value.strip("/")
    if value == "/":
        return ""
    return value


def web_gate_cookie_value(web_path: str) -> str:
    return hmac.new(
        SECRET_KEY.encode("utf-8"),
        web_path.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class WebGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        web_path = normalize_web_path(os.getenv("PANEL_WEB_PATH", ""))
        if not web_path:
            return await call_next(request)

        path = request.scope.get("path", "")
        if path.startswith("/api/") or path.startswith("/static/") or path in {"/favicon.ico"}:
            return await call_next(request)

        cookie_header = request.headers.get("cookie", "")
        cookie = SimpleCookie(cookie_header)
        expected_cookie = web_gate_cookie_value(web_path)
        has_gate_cookie = cookie.get(COOKIE_NAME) and cookie[COOKIE_NAME].value == expected_cookie

        if path == web_path or path.startswith(web_path + "/"):
            stripped = path[len(web_path):] or "/"
            request.scope["path"] = stripped
            request.scope["root_path"] = web_path
            response = await call_next(request)
            response.set_cookie(
                COOKIE_NAME,
                expected_cookie,
                httponly=True,
                samesite="lax",
                max_age=60 * 60 * 24 * 365,
            )
            return response

        if has_gate_cookie:
            return await call_next(request)

        return Response(status_code=404)
