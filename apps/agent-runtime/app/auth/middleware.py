"""Default-deny at the edge.

A dependency protects the routes that ask for it. This protects the ones that
forget. Anything outside ``PUBLIC_PATHS`` is refused **before routing**, so
opening an endpoint means editing a frozenset a reviewer will notice, rather
than omitting a line nobody will.

Two layers for one property, deliberately. ``route_audit`` catches a missing
dependency at boot; this catches it at request time. Either alone would be
enough on a good day, and the failure they guard against — a route shipped
without a principal — is exactly the kind that happens on a bad one.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.auth.route_audit import PUBLIC_PATHS
from app.auth.tokens import KeyServerUnavailable, TokenRejected, verify


def _raw_bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise TokenRejected("no bearer token")
    return token


class PrincipalMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        try:
            # Verified once here and stashed; `current_principal` reads the
            # stash so the token is not parsed twice per request.
            request.state.verified_token = verify(_raw_bearer(request))
        except KeyServerUnavailable:
            # 503, never 401. Our key server being down is not the caller's
            # credentials being wrong, and saying so sends them to reset a
            # password that was never the problem.
            return JSONResponse(
                {"detail": "authentication is temporarily unavailable"}, status_code=503
            )
        except TokenRejected:
            # Flat message. Which claim failed is the feedback a forger uses to
            # tune the next attempt.
            return JSONResponse({"detail": "invalid or expired token"}, status_code=401)

        return await call_next(request)
