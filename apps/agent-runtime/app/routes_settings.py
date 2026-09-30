"""What an organisation configures about itself: its own key, and its models.

Two routes' worth of surface and one asymmetry worth explaining.

`model_preferences` is written on the TENANT connection, so the owner-or-admin
rule is an RLS policy and this module does not restate it. `core.org_secrets` is
written on the SERVICE connection, because the ciphertext must not be
tenant-writable — a tenant that could UPDATE that table could replace another
organisation's encrypted key with its own, and every row would still look
correct. RLS cannot enforce a rule on a connection it does not apply to, so the
role check for that one is a written line here, and it is written where it can
be seen rather than implied.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from psycopg.errors import InsufficientPrivilege
from pydantic import BaseModel, Field

from app.auth.scope import AuthorizedWorkspace, authorized_workspace
from app.config import get_settings
from app.models.for_org import tier_choices
from app.models.router import RoutingConfig
from app.secrets import store as secrets

router = APIRouter()


def _require_admin(ws: AuthorizedWorkspace) -> None:
    """Owner or admin, read on the tenant connection.

    Not RLS, because the write this guards happens on the service connection.
    `core.org_role` answers only about the caller — asking it about anybody else
    has raised since 20260911000008 — so this is a question about the session
    rather than about a name the request supplied.
    """
    with ws.principal.tx() as cur:
        cur.execute("select core.org_role(%s::uuid)::text as role", (ws.org_id,))
        row = cur.fetchone()

    if (row or {}).get("role") not in ("owner", "admin"):
        raise HTTPException(403, "only an owner or an admin may change this")


# ---------------------------------------------------------------------------
# Bring your own key
# ---------------------------------------------------------------------------


class OpenRouterKey(BaseModel):
    # Not `SecretStr`. Pydantic's repr masking is a display convenience, and the
    # protection that matters here is that the value never leaves this process
    # in clear and never reaches a log - which is a property of what the code
    # does with it, not of how it prints.
    api_key: str = Field(min_length=16, max_length=512)


@router.get("/api/workspaces/{workspace_id}/settings/byok")
def key_status(
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
) -> dict[str, Any]:
    """Whether a key is stored, its last four characters, and whether it has
    ever worked. Never the key.

    "Never worked" and "stopped working" are different problems — a typo at
    setup versus a revocation or a spend limit — and a page that says only
    "invalid" cannot tell an owner which of their afternoons to spend on it.
    """
    return {"secrets": secrets.status(ws.org_id)}


@router.put("/api/workspaces/{workspace_id}/settings/byok")
def store_key(
    payload: OpenRouterKey,
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
) -> dict[str, Any]:
    """Encrypt and store this organisation's own OpenRouter key.

    Sealed with AES-256-GCM whose additional authenticated data is derived from
    (org_id, kind), so the row cannot be moved to another organisation and
    decrypted — which is what stops write access to one table becoming a way to
    spend somebody else's OpenRouter credit.
    """
    _require_admin(ws)

    try:
        stored = secrets.store(ws.org_id, payload.api_key)
    except secrets.SecretsNotConfigured as exc:
        # 503, not 500 and not 400. The caller's request is fine; this
        # deployment cannot encrypt, and storing the key in clear instead is the
        # one behaviour that would be worse than refusing.
        raise HTTPException(503, f"secret storage is not configured: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    return {"stored": stored}


# ---------------------------------------------------------------------------
# Model tiers, per function
# ---------------------------------------------------------------------------


class TierChoice(BaseModel):
    role: str
    tier: Literal["best", "value", "cheap"]


@router.get("/api/workspaces/{workspace_id}/settings/models")
def model_settings(
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
) -> dict[str, Any]:
    """The catalogue and this organisation's choices.

    `chosen` is deliberately absent for a function nobody has set, rather than
    pre-filled with the recommendation. Absent means "use whatever is
    recommended", which is what lets the recommendation be improved for every
    customer who has never opened this page — pre-filling would silently freeze
    each of them on today's answer.
    """
    config = RoutingConfig.load(get_settings().routing_config_path)
    chosen = tier_choices(ws.org_id)

    return {
        "functions": [
            {**tier.as_dict(), "chosen": chosen.get(tier.role)}
            for tier in config.tiers.values()
        ],
        "note": (
            "A function you have not set uses the recommended tier. Every call is "
            "billed to your own OpenRouter key."
        ),
    }


@router.put("/api/workspaces/{workspace_id}/settings/models")
def set_model_tier(
    payload: TierChoice,
    ws: Annotated[AuthorizedWorkspace, Depends(authorized_workspace)],
) -> dict[str, Any]:
    """Choose a tier for one function.

    On the TENANT connection, so `model_preferences_write` decides whether this
    caller may — spending more per call is a commercial decision about the
    organisation's own bill, and owners and admins make it.
    """
    config = RoutingConfig.load(get_settings().routing_config_path)
    tier = config.tier_for_role(payload.role)

    if tier is None:
        raise HTTPException(
            422,
            f"{payload.role!r} is not a configurable function; "
            f"choose from {sorted(t.role for t in config.tiers.values())}",
        )
    if tier.fixed:
        # Compliance adjudication. Refused with the reason rather than accepted
        # and ignored: a settings page that appeared to save a choice the system
        # then disregarded would be worse than one that says no.
        raise HTTPException(
            422,
            f"{tier.label} is not a customer choice. {tier.why_recommended}",
        )
    if payload.tier not in tier.options:
        raise HTTPException(
            422, f"{tier.label} offers {sorted(tier.options)}, not {payload.tier!r}"
        )

    with ws.principal.tx() as cur:
        try:
            cur.execute(
                """
                insert into t_advit.model_preferences (org_id, role, tier)
                values (%s::uuid, %s, %s::t_advit.model_tier)
                on conflict (org_id, role) do update set tier = excluded.tier
                returning role, tier::text as tier
                """,
                (ws.org_id, payload.role, payload.tier),
            )
        except InsufficientPrivilege as exc:
            # model_preferences_write's WITH CHECK refuses a member by RAISING,
            # not by returning no row - so the `row is None` check the first
            # version kept was dead, and a media buyer saving a tier got a
            # 500 rather than the 403 the settings page promises.
            raise HTTPException(403, "only an owner or an admin may change this") from exc
        row = cur.fetchone()

    if row is None:  # pragma: no cover - RETURNING on a successful upsert always yields the row
        raise HTTPException(403, "only an owner or an admin may change this")

    resolved = config.class_for_choice(payload.role, payload.tier)
    return {
        "role": row["role"],
        "tier": row["tier"],
        # What it actually resolves to, so the page can say "strategy will use
        # claude-opus-5" rather than only "you picked best".
        "model": resolved.primary,
        "class": resolved.name,
    }
