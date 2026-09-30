"""A model router bound to one organisation's key and one organisation's choices.

Two things are per-organisation and neither was before:

**The API key.** The product is sold on the customer supplying their own
OpenRouter key - they see their own spend and set their own limits, and this
business does not resell tokens. `ModelRouter` read one key from `Settings`, so
every organisation's calls were billed to whichever key the process started with.

**The tier per function.** `config/routing.yaml` routes by task class, which is
the right engineering abstraction and is not what a customer chooses. They choose
per function, between three options with one recommended.

There is deliberately no fallback to a platform key. An organisation with no key
gets an error naming the problem, because the alternative silently bills this
business for their usage and hides the gap until the invoice.
"""

from __future__ import annotations

from app.config import get_settings
from app.db.pools import service_conn
from app.models.router import ModelRouter, RoutingConfig
from app.secrets.store import NoKeyForOrganisation, resolve


def tier_choices(org_id: str) -> dict[str, str]:
    """What this organisation chose, per role. Absent roles are absent, not
    defaulted - the router resolves those to the recommendation, so a default
    written here would be a second place the recommendation lives."""
    with service_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "select role, tier::text as tier from t_advit.model_preferences where org_id = %s::uuid",
            (org_id,),
        )
        return {row["role"]: row["tier"] for row in cur.fetchall()}


def router_for_org(org_id: str, *, config: RoutingConfig | None = None) -> ModelRouter:
    """Raises NoKeyForOrganisation when the customer has not supplied a key.

    Callers that can degrade - the orchestrator, which produces facts and a
    compliance verdict with no model at all - should catch it and carry on
    without narration. Callers that cannot should let it surface: "you have not
    added your OpenRouter key" is an answer the owner can act on, and a silent
    empty response is not.
    """
    settings = get_settings()
    return ModelRouter(
        resolve(org_id),
        config or RoutingConfig.load(settings.routing_config_path),
        tier_choices=tier_choices(org_id),
    )


__all__ = ["NoKeyForOrganisation", "router_for_org", "tier_choices"]
