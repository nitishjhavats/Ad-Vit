"""Model routing (PRD 17.7, founder decision D1).

Routing is declarative config per agent role, not code. No agent names a model;
it names a ROLE, the role maps to a CLASS, and the class resolves to a model
plus a fallback chain. Following the frontier is then a config change and an
eval run, never a code change.

Three properties this layer owes the rest of the system:

* A provider outage degrades quality, never availability. Every class declares
  a fallback chain and the router walks it (PRD 14.6).

* Every call is metered. Tokens and cost land in core.usage_events, which is
  what turns "model cost under 15% of subscription revenue" from a target into
  a number on the superadmin dashboard (PRD 21.2).

* Arithmetic is never done by a model. Cost is computed here from the token
  counts the provider returns and the prices in config - not estimated, and
  never asked of an LLM.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx
import yaml

# Rupees per US dollar, used only to present cost in the owner's currency.
# A rate this coarse is fine for a cost dashboard and wrong for an invoice, so
# billing must never read it.
USD_TO_INR = 88.0


class ModelUnavailable(RuntimeError):
    """A class has no resolvable model. Raised rather than silently degrading -
    an embedding request answered by a chat model produces garbage."""


class AllModelsFailed(RuntimeError):
    def __init__(self, role: str, attempts: list[tuple[str, str]]) -> None:
        detail = "; ".join(f"{m}: {e}" for m, e in attempts)
        super().__init__(f"every model for role {role!r} failed - {detail}")
        self.role = role
        self.attempts = attempts


class OutputTruncated(RuntimeError):
    """The model hit max_tokens before finishing.

    Diagnosed separately from a parse failure because the remedy is different
    and the misdiagnosis is expensive. On a thinking model, max_tokens covers
    reasoning AND visible output: a request for a short structured answer can
    spend its whole budget reasoning and return truncated JSON. Reporting that
    as "malformed output" sends you hunting a schema bug that does not exist.
    """

    def __init__(self, model: str, tokens_out: int, reasoning_tokens: int) -> None:
        visible = tokens_out - reasoning_tokens
        super().__init__(
            f"{model} hit max_tokens: {tokens_out} completion tokens, of which "
            f"{reasoning_tokens} were reasoning, leaving {visible} for the answer. "
            "Raise max_tokens - on a thinking model the budget covers both."
        )
        self.model = model
        self.tokens_out = tokens_out
        self.reasoning_tokens = reasoning_tokens


# Thinking models spend part of max_tokens before producing a visible token.
# A structured call asking for a small object still needs room for that, so
# these floors apply when the caller has not asked for something larger.
MIN_MAX_TOKENS_THINKING = 2000
MIN_MAX_TOKENS_STRUCTURED = 3000


@dataclass(frozen=True, slots=True)
class Tier:
    """One function, in the customer's vocabulary, with the choice attached.

    `options` maps a tier name to a CLASS name rather than to a model id -
    the classes already carry verified models, fallback chains and prices, and a
    tier naming a raw model id would be a fourth place model names live.

    `fixed` means the customer does not get to choose. Exactly one function is
    fixed and the reason is written beside it in config/routing.yaml.
    """

    key: str
    label: str
    role: str
    recommended: str
    fixed: bool
    why_recommended: str
    options: dict[str, str]
    notes: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        """What a settings page renders. Includes the recommendation and the
        reason for it, because a choice offered without one is a choice the
        customer makes on price alone."""
        return {
            "key": self.key,
            "label": self.label,
            # The PUT resolves a choice by ROLE (tier_for_role), and the key is
            # not the role for the main function: the "chat" tier configures
            # the "orchestrator" role. A settings page that had only the key
            # sent "chat" and was refused with a list of valid roles - a form
            # that could never save for the product's main function.
            "role": self.role,
            "recommended": self.recommended,
            "why_recommended": self.why_recommended,
            "fixed": self.fixed,
            "options": [
                {
                    "tier": name,
                    "note": self.notes.get(name, ""),
                    "recommended": name == self.recommended,
                }
                for name in ("best", "value", "cheap")
                if name in self.options
            ],
        }


@dataclass(frozen=True, slots=True)
class ModelClass:
    name: str
    primary: str | None
    fallbacks: tuple[str, ...]
    price_in: float
    price_out: float
    max_tokens: int
    temperature: float
    unavailable_reason: str | None = None

    def chain(self) -> tuple[str, ...]:
        if self.primary is None:
            return ()
        return (self.primary, *self.fallbacks)


@dataclass(slots=True)
class Completion:
    text: str
    model: str
    role: str
    model_class: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    cost_inr: float
    latency_ms: int
    attempts: list[str] = field(default_factory=list)
    fell_back: bool = False
    # Billed as completion tokens, so already inside tokens_out. Tracked
    # separately because it is the difference between "the model was verbose"
    # and "the model thought hard", which are different cost problems.
    reasoning_tokens: int = 0
    finish_reason: str = ""
    # True when cost came from the provider rather than from our price table.
    cost_is_reported: bool = False

    @property
    def visible_tokens_out(self) -> int:
        return max(0, self.tokens_out - self.reasoning_tokens)

    def as_usage_events(self) -> list[dict[str, Any]]:
        """Rows for core.usage_events. Split in/out because they price
        differently and a blended figure hides which half is expensive."""
        return [
            {
                "metric_key": "model.tokens_in",
                "quantity": self.tokens_in,
                "unit_cost_inr": (self.cost_inr / self.tokens_in) if self.tokens_in else 0,
            },
            {
                "metric_key": "model.tokens_out",
                "quantity": self.tokens_out,
                "unit_cost_inr": 0,
            },
        ]


class RoutingConfig:
    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw
        self.base_url: str = raw.get("base_url", "https://openrouter.ai/api/v1")
        self.referer: str = raw.get("referer", "")
        self.title: str = raw.get("title", "")
        self.roles: dict[str, str] = dict(raw.get("roles", {}))
        self.tiers: dict[str, Tier] = {}
        for name, spec in (raw.get("tiers") or {}).items():
            options = {
                key: str((value or {}).get("class", ""))
                for key, value in (spec.get("options") or {}).items()
            }
            self.tiers[name] = Tier(
                key=name,
                label=str(spec.get("label", name)),
                role=str(spec.get("role", name)),
                recommended=str(spec.get("recommended", "value")),
                fixed=bool(spec.get("fixed", False)),
                why_recommended=" ".join(str(spec.get("why_recommended", "")).split()),
                options=options,
                notes={
                    key: str((value or {}).get("note", ""))
                    for key, value in (spec.get("options") or {}).items()
                },
            )
        self.batch_eligible: frozenset[str] = frozenset(raw.get("batch_eligible_roles", []))
        self.max_tokens_per_run: int = int(
            raw.get("budgets", {}).get("max_tokens_per_run", 200_000)
        )

        self.classes: dict[str, ModelClass] = {}
        for name, spec in (raw.get("classes") or {}).items():
            pricing = spec.get("pricing_per_mtok") or {}
            self.classes[name] = ModelClass(
                name=name,
                primary=spec.get("primary"),
                fallbacks=tuple(spec.get("fallbacks") or ()),
                price_in=float(pricing.get("in", 0.0)),
                price_out=float(pricing.get("out", 0.0)),
                max_tokens=int(spec.get("max_tokens", 4000)),
                temperature=float(spec.get("temperature", 0.2)),
                unavailable_reason=spec.get("unavailable_reason"),
            )

    @classmethod
    def load(cls, path: str | Path) -> "RoutingConfig":
        p = Path(path)
        if not p.is_absolute():
            for parent in Path(__file__).resolve().parents:
                candidate = parent / "config" / "routing.yaml"
                if candidate.exists():
                    p = candidate
                    break
        if not p.exists():
            raise FileNotFoundError(f"routing config not found at {p}")
        return cls(yaml.safe_load(p.read_text(encoding="utf-8")))

    # -- tiers ------------------------------------------------------------

    def tier_for_role(self, role: str) -> Tier | None:
        for tier in self.tiers.values():
            if tier.role == role:
                return tier
        return None

    def class_for_choice(self, role: str, choice: str | None) -> ModelClass:
        """Resolve a role to a class, honouring an organisation's chosen tier.

        `choice` of None means the organisation has not chosen, and that is the
        common case rather than an edge one: absent means "use the recommended
        tier", so improving a recommendation reaches every customer who has
        never opened the settings page without touching a single row.

        A choice this file does not offer falls back to the recommendation
        rather than raising. The alternative is that removing a tier from the
        catalogue breaks every organisation that had selected it, at the moment
        of the deploy, on every call - and the honest behaviour for "you chose
        something that no longer exists" is the default, not an outage.
        """
        tier = self.tier_for_role(role)
        if tier is None:
            return self.class_for(role)

        if tier.fixed:
            # Compliance adjudication. Not a customer choice: offering a
            # cheaper model for the check that keeps an advertiser legal is
            # offering them a discount on their own compliance.
            selected = tier.recommended
        else:
            selected = choice if choice in tier.options else tier.recommended

        class_name = tier.options.get(selected)
        if not class_name:
            return self.class_for(role)

        cls_ = self.classes.get(class_name)
        if cls_ is None or not cls_.chain():
            return self.class_for(role)
        return cls_

    def class_for(self, role: str) -> ModelClass:
        class_name = self.roles.get(role)
        if class_name is None:
            raise ModelUnavailable(
                f"role {role!r} is not in the routing table; add it to config/routing.yaml "
                "rather than naming a model in code"
            )
        cls_ = self.classes.get(class_name)
        if cls_ is None:
            raise ModelUnavailable(f"class {class_name!r} referenced by role {role!r} is undefined")
        if not cls_.chain():
            raise ModelUnavailable(
                cls_.unavailable_reason
                or f"class {class_name!r} resolves to no model"
            )
        return cls_


class ModelRouter:
    """Calls models through OpenRouter, walking the fallback chain."""

    def __init__(
        self,
        api_key: str,
        config: RoutingConfig,
        *,
        timeout_s: float = 120.0,
        client: httpx.Client | None = None,
        tier_choices: dict[str, str] | None = None,
    ) -> None:
        self._api_key = api_key
        self._config = config
        self._timeout = timeout_s
        self._client = client
        # Which tier this organisation chose, per role. Empty means "nobody has
        # chosen", which resolves to the recommendation in config/routing.yaml -
        # and that is the common case, not an edge one.
        self._tier_choices = dict(tier_choices or {})

    @property
    def config(self) -> RoutingConfig:
        return self._config

    def _http(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(timeout=self._timeout)

    def complete(
        self,
        role: str,
        *,
        system: str,
        # A string, or a list of content parts for a multimodal turn -
        # {"type": "text", ...} and {"type": "image_url", ...} in the OpenAI
        # shape OpenRouter forwards. The creative studio sends frames this way;
        # every other caller sends a string and nothing about them changes.
        user: str | list[dict[str, Any]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_schema: dict[str, Any] | None = None,
        stable_prefix: str | None = None,
    ) -> Completion:
        """Run one completion for a role, falling back on failure.

        ``stable_prefix`` is content that repeats across calls - the industry
        pack, the policy rules, the account digest. It is placed first and
        marked for caching, which is the single largest cost lever available
        (PRD 19.2).
        """
        if not self._api_key:
            raise ModelUnavailable(
                "no OPENROUTER_API_KEY configured; set one or run the deterministic paths"
            )

        # class_for_choice, not class_for: the organisation's own tier decides,
        # falling back to the recommendation when it has not chosen and when it
        # chose something the catalogue no longer offers.
        cls_ = self._config.class_for_choice(role, self._tier_choices.get(role))
        messages: list[dict[str, Any]] = []

        # ORDER MATTERS, and it used to be wrong.
        #
        # `stable_prefix` is the account digest: rows from
        # t_advit.account_context, which the tenant can write. It was placed
        # FIRST, ahead of the `system` message that frames it as retrieved data
        # rather than as instructions - so the first thing the model read was
        # attacker-influenceable text with nothing yet telling it what that text
        # was. The compliance gate has already been bitten once by reading
        # account_context as authority; this is the same table reaching the same
        # model by a different route.
        #
        # The rules go first. The cache breakpoint moves onto the second block,
        # which means the cached prefix is now [system, stable_prefix] rather
        # than [stable_prefix] alone - still stable per (role, workspace), so
        # PRD 19.2's largest cost lever is unaffected. The `system` text is a
        # constant per role and changes only on deploy.
        messages.append({"role": "system", "content": system})
        if stable_prefix:
            messages.append(
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": stable_prefix,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            )
        messages.append({"role": "user", "content": user})

        # Floor the budget: on a thinking model max_tokens covers reasoning as
        # well as the visible answer, so a small ceiling truncates the answer
        # rather than shortening it.
        effective_max = max_tokens or cls_.max_tokens
        floor = MIN_MAX_TOKENS_STRUCTURED if response_schema is not None else MIN_MAX_TOKENS_THINKING
        effective_max = max(effective_max, floor)

        body: dict[str, Any] = {
            "messages": messages,
            "max_tokens": effective_max,
            "temperature": cls_.temperature if temperature is None else temperature,
            "usage": {"include": True},
        }
        if response_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "strict": True,
                    "schema": response_schema,
                },
            }

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._config.referer:
            headers["HTTP-Referer"] = self._config.referer
        if self._config.title:
            headers["X-Title"] = self._config.title

        attempts: list[tuple[str, str]] = []
        client = self._http()
        owns_client = self._client is None

        try:
            for index, model in enumerate(cls_.chain()):
                started = time.monotonic()
                try:
                    response = client.post(
                        f"{self._config.base_url}/chat/completions",
                        headers=headers,
                        json={**body, "model": model},
                    )
                    if response.status_code >= 400:
                        attempts.append((model, f"HTTP {response.status_code}: {response.text[:180]}"))
                        continue

                    payload = response.json()
                    if "error" in payload:
                        attempts.append((model, str(payload["error"])[:180]))
                        continue

                    choice = payload["choices"][0]
                    text = choice["message"].get("content") or ""
                    finish = choice.get("finish_reason") or ""

                    usage = payload.get("usage") or {}
                    tokens_in = int(usage.get("prompt_tokens", 0))
                    tokens_out = int(usage.get("completion_tokens", 0))
                    reasoning = int(
                        (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
                    )

                    # Prefer the provider's own figure: it already accounts for
                    # cache discounts and per-provider variation. Fall back to
                    # the price table only when it is absent. Either way the
                    # arithmetic happens here, never in a model.
                    reported = usage.get("cost")
                    if reported is not None:
                        cost_usd, cost_reported = float(reported), True
                    else:
                        cost_usd = (
                            tokens_in / 1_000_000 * cls_.price_in
                            + tokens_out / 1_000_000 * cls_.price_out
                        )
                        cost_reported = False

                    completion = Completion(
                        text=text,
                        model=model,
                        role=role,
                        model_class=cls_.name,
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        cost_usd=cost_usd,
                        cost_inr=cost_usd * USD_TO_INR,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        attempts=[m for m, _ in attempts] + [model],
                        fell_back=index > 0,
                        reasoning_tokens=reasoning,
                        finish_reason=finish,
                        cost_is_reported=cost_reported,
                    )

                    # A truncated answer is a failed call, not a partial one.
                    # Try the next model rather than handing back half an
                    # object that a downstream parser will misdiagnose.
                    if finish == "length":
                        attempts.append(
                            (model, f"truncated at max_tokens ({reasoning} reasoning tokens)")
                        )
                        if index == len(cls_.chain()) - 1:
                            raise OutputTruncated(model, tokens_out, reasoning)
                        continue

                    return completion
                except OutputTruncated:
                    raise
                except (httpx.HTTPError, KeyError, ValueError) as exc:
                    attempts.append((model, f"{type(exc).__name__}: {exc}"))
                    continue
        finally:
            if owns_client:
                client.close()

        raise AllModelsFailed(role, attempts)

    def complete_json(
        self,
        role: str,
        *,
        system: str,
        user: str | list[dict[str, Any]],
        schema: dict[str, Any],
        stable_prefix: str | None = None,
        max_tokens: int | None = None,
    ) -> tuple[dict[str, Any], Completion]:
        """Structured output. Returns the parsed object and the completion.

        A model that returns unparseable JSON is a failed call, not a partial
        one: never let a malformed structured output become an API call
        (PRD 14.6).
        """
        completion = self.complete(
            role,
            system=system,
            user=user,
            response_schema=schema,
            stable_prefix=stable_prefix,
            max_tokens=max_tokens,
        )
        text = completion.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AllModelsFailed(
                role, [(completion.model, f"unparseable JSON: {exc}")]
            ) from exc

        # Parseable is not the same as usable, and this function is annotated
        # `-> tuple[dict[str, Any], Completion]`.
        #
        # json.loads returns a list for "[]", a str for "\"done\"" and None for
        # "null". All three are valid JSON documents, none is a structured
        # output, and all three used to be handed back as one. The strategy node
        # then called .get() on it and raised AttributeError several frames from
        # the model call that caused it - so a provider returning the wrong shape
        # surfaced as a crash inside a graph node rather than as a failed
        # completion the fallback chain could describe.
        if not isinstance(parsed, dict):
            raise AllModelsFailed(
                role,
                [(completion.model, f"expected a JSON object, got {type(parsed).__name__}")],
            )

        # The schema was already being SENT to the provider (as
        # response_format.json_schema.schema) and never used to check what came
        # back, which made `strict: True` a request rather than a guarantee.
        #
        # This checks the top-level required keys and nothing else - not types,
        # not nested shape. Said plainly so nobody reads it as validation: it
        # catches the failure that actually happens, which is a model wrapping
        # its answer in another object or dropping a field under token pressure.
        # Full JSON Schema validation would mean a new dependency for one
        # function.
        missing = [
            key
            for key in (schema.get("required") or [])
            if isinstance(key, str) and key not in parsed
        ]
        if missing:
            raise AllModelsFailed(
                role,
                [(
                    completion.model,
                    "structured output is missing required key(s): " + ", ".join(missing),
                )],
            )

        return parsed, completion


def record_usage(
    *,
    org_id: str,
    product_id: str,
    completions: Iterable[Completion],
    workspace_id: str | None = None,
    run_id: str | None = None,
) -> int:
    """Meter completions into core.usage_events.

    Revenue lives in core.invoices; this is the other half of the margin view.
    """
    from app.db.pools import service_conn

    rows = 0
    with service_conn() as conn, conn.cursor() as cur:
        for c in completions:
            cur.execute(
                """
                insert into core.usage_events
                  (org_id, product_id, metric_key, quantity, unit_cost_inr,
                   workspace_id, run_id, ref_json)
                values (%s, %s, 'model.tokens_in',  %s, %s, %s, %s, %s::jsonb),
                       (%s, %s, 'model.tokens_out', %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    org_id, product_id, c.tokens_in,
                    (c.cost_inr / c.tokens_in) if c.tokens_in else 0,
                    workspace_id, run_id,
                    json.dumps({"model": c.model, "role": c.role, "class": c.model_class,
                                "fell_back": c.fell_back, "latency_ms": c.latency_ms}),
                    org_id, product_id, c.tokens_out, 0,
                    workspace_id, run_id,
                    json.dumps({"model": c.model, "role": c.role, "class": c.model_class}),
                ),
            )
            rows += 2
        conn.commit()
    return rows
