"""Runtime configuration.

Everything that decides how much damage the system can do is configuration, not
code (PRD Appendix E: if turning something off requires a deploy, it is not a
safety control).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"), extra="ignore", case_sensitive=False
    )

    # The superuser DSN. Kept for `supabase db reset` tooling and the test
    # suites' own fixture setup, and read by NO runtime code path -
    # tests/test_service_connection_surface.py enforces that. On this
    # connection RLS is not weakened, it is absent.
    database_url: str = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"

    # --- The two connections the runtime actually holds -------------------
    #
    # Created NOLOGIN by 20260911000007; given local passwords by
    # supabase/seeds/05_runtime_roles_local.sql, and production passwords by a
    # Coolify pre-deploy step, because a migration in git must not carry a
    # secret. These defaults reach 127.0.0.1 only.
    tenant_database_url: str = (
        "postgresql://advit_tenant:advit_tenant_local@127.0.0.1:54322/postgres"
    )
    service_database_url: str = (
        "postgresql://advit_service:advit_service_local@127.0.0.1:54322/postgres"
    )

    # --- Supabase Auth ----------------------------------------------------
    #
    # The JWT is verified locally against these. A per-request call to
    # /auth/v1/user would tie this API's availability to the auth service, add
    # a network hop to the hot path, and be rate-limited.
    supabase_url: str = "http://127.0.0.1:54321"
    supabase_jwt_issuer: str = ""            # defaults to f"{supabase_url}/auth/v1"
    supabase_jwt_secret: str = ""            # HS256, today's self-hosted shape
    supabase_jwt_secret_previous: str = ""   # so an HMAC rotation has a window
    supabase_jwks_url: str = ""              # set when auth.signing_keys_path is on

    # --- Supabase Storage -------------------------------------------------
    #
    # The one credential here that is NOT a Postgres role. It authenticates
    # the runtime to the Storage SERVICE (signed upload URLs, downloads for
    # analysis), which enforces its own bucket policies. It never reaches the
    # connection pools, so it has nothing to do with the BYPASSRLS question the
    # runtime roles migration answered. Scoped out of apps/web on purpose - the
    # browser uploads through a signed URL this key minted, never with the key.
    supabase_service_role_key: str = ""

    # --- The tenant web app ------------------------------------------------
    #
    # Where an invitation link lands. GoTrue's own action_link points at the
    # auth service, which verifies the token and then redirects to ITS
    # site_url with the session in a URL fragment - a shape only a browser
    # client reads, and this product has none. So app/auth/invite.py hands out
    # {web_base_url}/auth/confirm?token_hash=...&type=invite instead, and the
    # web app's confirm page verifies the hash server-side and sets the
    # cookie. Env: WEB_BASE_URL. Production is https://ad-vit.broadmate.org;
    # local is http://localhost:3000, and it is NOT the default: a runtime
    # that forgot the variable must refuse to invite, not hand an owner a
    # link to somebody's laptop. app/auth/invite.py treats "" as "cannot".
    web_base_url: str = ""

    # --- Invoicing --------------------------------------------------------
    #
    # In the environment template since the first commit; read by nothing until
    # app/billing/invoices.py. The seller's own GSTIN and state code decide the
    # CGST+SGST / IGST split, and without them an invoice cannot be issued -
    # it is left as a draft with the reason in the job record, never issued
    # with the wrong tax on it.
    gst_rate_percent: float = 18.0
    gst_sac_code: str = "998314"
    seller_gstin: str = ""
    seller_legal_name: str = ""
    seller_state_code: str = ""
    invoice_number_prefix: str = "BMG"

    # --- Collecting the money ----------------------------------------------
    #
    # There is no payment gateway. An owner asks to pay an issued invoice, is
    # shown where to send the money, and has this many hours to send it and
    # quote the UTR / UPI reference; an operator then matches the reference
    # to the bank statement (app/billing/payments.py, routes_billing.py).
    # In the environment template since the first commit; read by nothing
    # until now.
    payment_window_hours: int = Field(default=4, ge=1, le=168)

    # Where the money goes. These are the RUNTIME's, not the jobs process's:
    # they are shown to a signed-in owner at request time, and the jobs
    # process shows nothing to anybody. Until at least a UPI id or a full
    # bank triple (account name, number, IFSC) is set, pay_to() is None and
    # a payment request is refused with 503 before anything is written -
    # an open request with nowhere to send the money is a deadline the
    # customer cannot meet.
    seller_bank_account_name: str = ""
    seller_bank_account_number: str = ""
    seller_bank_ifsc: str = ""
    seller_bank_name: str = ""
    seller_upi_id: str = ""

    # --- Meta gateway -----------------------------------------------------
    # fixture is the default deliberately: it cannot reach the network and
    # therefore cannot spend money.
    meta_driver: str = "fixture"
    meta_api_version: str = "v26.0"
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_access_token: str = ""

    # Second guard behind meta_connections.write_enabled. An account must
    # appear in BOTH to be writable; the allowlist is the operator's switch and
    # the column is the product's.
    meta_write_allowlist: str = "1000000000000003"

    # --- Models -----------------------------------------------------------
    #
    # Kept for local development and for the test suite. Production routes
    # through app/models/for_org.py, which resolves the CUSTOMER's key from
    # core.org_secrets - the product is sold on bring-your-own-key, and a
    # platform key that silently served every tenant would bill this business
    # for their usage and hide the gap until the invoice.
    openrouter_api_key: str = ""
    anthropic_api_key: str = ""
    routing_config_path: str = "../../config/routing.yaml"

    # --- Observability ----------------------------------------------------
    langfuse_host: str = "http://127.0.0.1:3030"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    otel_service_name: str = "advit-agent-runtime"

    # --- Secrets ----------------------------------------------------------
    #
    # Base64, 32 bytes decoded. Read from the environment by
    # app/secrets/envelope.py rather than through Settings, because
    # pydantic-settings repr()s its fields and a master key in a traceback is a
    # master key in a log aggregator.
    #
    #   python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"

    # --- Safety -----------------------------------------------------------
    approval_ttl_hours: int = Field(default=24, ge=1, le=168)
    mutation_lock_timeout_s: int = Field(default=30, ge=1, le=300)
    run_timeout_s: int = Field(default=900, ge=30)

    @field_validator("meta_driver")
    @classmethod
    def _known_driver(cls, v: str) -> str:
        allowed = {"fixture", "graph", "adsmcp"}
        if v not in allowed:
            raise ValueError(f"meta_driver must be one of {sorted(allowed)}, got {v!r}")
        return v

    @property
    def write_allowlist(self) -> frozenset[str]:
        return frozenset(a.strip() for a in self.meta_write_allowlist.split(",") if a.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
