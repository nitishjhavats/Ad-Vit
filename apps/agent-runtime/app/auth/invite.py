"""Inviting a person who has no account yet, through GoTrue's admin API.

Onboarding names an owner by email. When that email is already a
``core.platform_users`` row the organisation simply gets a member. When it is
not, somebody has to create the auth user, and the only party that may do that
is Supabase Auth itself: ``core.platform_users`` is provisioned by the
``on_auth_user_created`` trigger, so there is no honest way to insert one from
this side.

This module is the one place the runtime talks to GoTrue. It is HTTP to the
auth SERVICE with the service-role key, exactly as ``app.creative.storage``
talks to the Storage service - the key never reaches a database connection, so
``routes_admin``'s promise that nothing there escapes row-level security holds:
a call here creates an auth user and nothing else.

It refuses rather than guesses. Without ``SUPABASE_SERVICE_ROLE_KEY`` there is
no credential that may create a user, and the caller is told so before it has
written anything; a GoTrue refusal is surfaced with its own words rather than
retried or worked around. What comes back is a link the operator sends by hand
- no SMTP is configured anywhere in this product, and a link that was returned
to a screen is at least a link somebody saw.

Which link matters. GoTrue's own ``action_link`` is ``/auth/v1/verify?token=``
on the auth service: opened, GoTrue verifies the token and redirects to its
``site_url`` with the session in a URL FRAGMENT, which only a browser-side
client can read - and apps/web has none by design (the token lives in an
httpOnly cookie). An owner who opened that link would land on a page that
reads nothing and be signed in nowhere. So the link handed out is composed
here from the ``hashed_token`` GoTrue returns beside it:
``{WEB_BASE_URL}/auth/confirm?token_hash=...&type=invite``, the documented
server-side pattern - the web app's route handler exchanges the hash for a
session with ``verifyOtp`` and sets the cookie itself. GoTrue's link is kept
on the dataclass for a debugging eye and goes no further: the route does not
return it, because a second link beside the right one is a link somebody
sends. A response without ``hashed_token`` is refused: there is nothing to
build a working link from, and an invitation nobody can accept is not one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import get_settings

# The two GoTrue link types this product hands out, and what each is for.
#
#   invite    an address with no auth user. GoTrue creates the user; the
#             trigger provisions core.platform_users. Refused by GoTrue for an
#             address it already has ("already registered").
#   recovery  an account that exists: an invitation that expired unsent, an
#             owner who forgot their password. GoTrue issues a hash for the
#             existing user and creates nothing. The web app's confirm page
#             accepts both types and sends both to /auth/set-password, so
#             the owner's experience is the same link either way.
#
# Not `magiclink`: it also works for an existing user, but /auth/confirm
# would sign them in and send them to set a password they may already have,
# and an operator issuing a sign-in link for somebody else should not be
# handing out a link that skips the password entirely. Recovery is the type
# whose name says what the link permits.
INVITE = "invite"
RECOVERY = "recovery"
LINK_TYPES = frozenset({INVITE, RECOVERY})


class InviteUnavailable(RuntimeError):
    """No credential with which to invite. The caller must not have created
    anything it would now be unable to hand to an owner."""


class InviteRefused(RuntimeError):
    """GoTrue answered, and the answer was no. Its message is carried verbatim
    because "already registered" and "invalid email" need different next steps
    from the operator."""


@dataclass(frozen=True, slots=True)
class Invitation:
    user_id: str
    email: str
    # The link that works: our /auth/confirm with GoTrue's token hash. This is
    # what the operator sends.
    link: str
    # GoTrue's own /auth/v1/verify link, verbatim. Kept for debugging - it
    # proves what GoTrue issued - and is not the thing to send, for the reason
    # in the module docstring.
    action_link: str


def confirm_link(hashed_token: str, verification_type: str) -> str:
    """The URL the owner opens: the web app's confirm handler, carrying the
    hash and the type it must be verified as. Query-encoded rather than
    interpolated so a token character that means something in a URL cannot
    change what the handler reads."""
    settings = get_settings()
    query = urlencode({"token_hash": hashed_token, "type": verification_type})
    return f"{settings.web_base_url.rstrip('/')}/auth/confirm?{query}"


def missing_invite_configuration() -> tuple[str, ...]:
    """The environment variables without which no invitation can be issued,
    by name, in the order the operator should set them. Empty means "go".

    Both are needed and neither is defaulted: without the key nothing may
    create a user, and without the base URL there is nowhere for the link to
    point - a default of localhost would have composed, on a production
    runtime that forgot the variable, an invitation to somebody's laptop."""
    settings = get_settings()
    missing: list[str] = []
    if not settings.supabase_service_role_key:
        missing.append("SUPABASE_SERVICE_ROLE_KEY")
    if not settings.web_base_url.strip():
        missing.append("WEB_BASE_URL")
    return tuple(missing)


def invites_are_possible() -> bool:
    """Whether an invitation could be issued at all. Asked BEFORE an
    onboarding transaction writes a row, so a missing variable refuses the
    whole act instead of half of it."""
    return not missing_invite_configuration()


def _call_gotrue(url: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The one network call. Separated so a test can replace it with a fake
    GoTrue and never reach a real one."""
    try:
        response = httpx.post(
            url,
            json=payload,
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        raise InviteUnavailable(f"could not reach Supabase Auth: {exc}") from exc
    if response.status_code >= 400:
        raise InviteRefused(
            f"Supabase Auth refused the invitation: HTTP {response.status_code} "
            f"{response.text[:300]}"
        )
    body = response.json()
    if not isinstance(body, dict):
        raise InviteRefused("Supabase Auth answered with something other than a user")
    return body


def generate_invite(email: str) -> Invitation:
    """``POST /auth/v1/admin/generate_link`` with ``type: invite``.

    GoTrue creates the auth user (the trigger provisions ``core.platform_users``
    in the same statement) and returns the user merged with, at top level,
    ``action_link``, ``hashed_token``, ``verification_type`` and
    ``redirect_to``. Nothing is emailed by anyone: the link composed from
    ``hashed_token`` is the whole result.
    """
    return generate_link(email, INVITE)


def generate_sign_in_link(email: str) -> Invitation:
    """``type: recovery`` for an account GoTrue already has. Creates nothing;
    the link signs the holder in once and asks for a password. For the
    invitation that expired before it was sent, and the owner who forgot."""
    return generate_link(email, RECOVERY)


def generate_link(email: str, kind: str) -> Invitation:
    """The one generator. ``kind`` is one of LINK_TYPES; anything else is a
    programming error, not a GoTrue question."""
    if kind not in LINK_TYPES:
        raise ValueError(f"{kind!r} is not a link type this product issues; one of {sorted(LINK_TYPES)}")
    settings = get_settings()
    missing = missing_invite_configuration()
    if missing:
        raise InviteUnavailable(
            f"{' and '.join(missing)} not configured, so the runtime cannot issue a {kind} link"
        )
    body = _call_gotrue(
        f"{settings.supabase_url.rstrip('/')}/auth/v1/admin/generate_link",
        settings.supabase_service_role_key,
        {"type": kind, "email": email},
    )
    user_id = str(body.get("id") or "")
    action_link = str(body.get("action_link") or "")
    hashed_token = str(body.get("hashed_token") or "")
    # An answer that names no user or no link is not an invitation, whatever
    # the status code said; treating it as one would attach memberships to
    # nobody.
    if not user_id or not action_link:
        raise InviteRefused("Supabase Auth returned no user id or no action link for the invitation")
    # And one without the hash is an invitation nobody can accept: the only
    # link we could hand out would be GoTrue's, which lands nowhere. Refused
    # here, before the route has written a row, so the retry starts clean.
    if not hashed_token:
        raise InviteRefused(
            "Supabase Auth returned no hashed_token for the invitation, so no link that "
            "reaches the web app's /auth/confirm can be composed"
        )
    return Invitation(
        user_id=user_id,
        email=str(body.get("email") or email),
        link=confirm_link(hashed_token, kind),
        action_link=action_link,
    )
