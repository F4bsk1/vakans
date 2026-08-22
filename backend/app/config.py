"""Application settings.

One source of truth for every knob. Two rules worth stating up front:

1. `database_url` is normalised so that pointing at Supabase EU really is a single env-var
   change (charter §3: "Migration path: Aiven/Scaleway/self-hosted at production").
2. Credentials are `SecretStr` and never logged. Log `safe_database_url` instead.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "db", "host.docker.internal"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="VAKANS_",
        extra="ignore",
        case_sensitive=False,
    )

    env: Literal["local", "test", "demo"] = "local"

    # --- database ------------------------------------------------------------------------
    # AliasChoices because hosting platforms inject a bare DATABASE_URL.
    database_url: str = Field(
        default="postgresql+psycopg://vakans:vakans@localhost:55432/vakans",
        validation_alias=AliasChoices("VAKANS_DATABASE_URL", "DATABASE_URL"),
    )
    test_database_name: str = "vakans_test"
    db_echo: bool = False

    # --- locale --------------------------------------------------------------------------
    default_timezone: str = "Europe/Stockholm"

    # --- LLM (charter D2: keep this behind a provider abstraction, it is a config change) --
    # Ingestion extraction only. This is offline batch work over *public* clinic web pages,
    # not patient data, so the provider choice here is not the EU-posture decision — that one
    # lives on the voice path (charter §2, D1).
    llm_provider: Literal["fake", "azure", "openai", "anthropic", "bedrock", "mistral"] = "fake"
    llm_model: str = "gpt-4.1"
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: SecretStr | None = None
    azure_openai_api_version: str = "2024-10-21"
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    aws_region: str = "eu-north-1"
    mistral_api_key: SecretStr | None = None

    # --- SMS (step 3) --------------------------------------------------------------------
    sms_provider: Literal["fake", "46elks"] = "fake"
    elks_username: str | None = None
    elks_password: SecretStr | None = None
    sms_sender_id: str = "Vakans"  # 46elks alphanumeric sender, max 11 chars. Charter D7.
    founder_mobile: str | None = None  # the "JA-svararen" — the ONE real number we may text

    # --- refill cascade (step 3) ----------------------------------------------------------
    # Spec §3: "Timeout 90 s → nästa kandidat." Configurable because a rehearsal in front of a
    # customer sometimes wants the cascade to advance in 30 rather than stand in silence.
    refill_offer_timeout_s: int = 90
    refill_worker_enabled: bool = True
    # How often the worker drains the SMS outbox and advances timed-out cascades. Well under
    # the timeout, so "90 seconds" is never observed as 130.
    refill_worker_interval_s: float = 5.0

    # --- demo sandbox --------------------------------------------------------------------
    seed_rng_seed: int = 20260907
    demo_token: SecretStr | None = None  # guards the destructive /sim/* endpoints
    # Guards /agent/*. Required outside tests: these endpoints are reached through a public
    # tunnel so the voice platform can call them, and unauthenticated they let anyone who
    # finds the URL book or cancel appointments.
    agent_token: SecretStr | None = None

    # --- voice (charter D1: ElevenLabs Agents) --------------------------------------------
    # The agent's id, written into elevenlabs/agents.json so their CLI updates rather than
    # creates. Their CLI reads its own credential from ELEVENLABS_API_KEY, so we do not hold
    # an ElevenLabs key at all any more.
    elevenlabs_agent_id: str | None = None
    # Which model runs the conversation. One env var rather than a code change, because the
    # right answer here is found by making calls and listening: a stronger model reasons better
    # about a loose prompt, a faster one keeps turns inside the 1.5 s budget. Sonnet is the
    # balance; try claude-haiku-4-5 if turns feel slow, claude-opus-4-7 if it feels stupid.
    elevenlabs_llm: str = "claude-sonnet-4-5"
    # Optional. If a workspace secret has been created in ElevenLabs, put its **id** here and
    # the generated tools reference it instead of carrying the literal token. Left unset, the
    # token is written into elevenlabs/tool_configs/ — which is gitignored, and holds the same
    # value .env already does on the same disk.
    elevenlabs_secret_id: str | None = None
    # Where the voice platform reaches this API. The tunnel URL in development.
    public_base_url: str = "http://localhost:8000"

    # --- dashboard (step 5) ---------------------------------------------------------------
    # The Next.js dashboard runs on its own origin in development, so the browser needs CORS to
    # call this API. Comma-separated; keep it to the origins that actually serve the dashboard
    # rather than "*", because /sim/* is destructive and reachable from a browser.
    dashboard_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # --- ingestion -----------------------------------------------------------------------
    crawl_max_pages: int = 40
    crawl_max_depth: int = 2
    crawl_user_agent: str = "VakansBot/0.1 (+https://vakans.se/bot)"
    crawl_respect_robots: bool = True
    artifacts_dir: Path = Path("artifacts")

    @field_validator("database_url")
    @classmethod
    def _normalise_database_url(cls, v: str) -> str:
        """Force the async psycopg driver and require SSL for non-local hosts.

        Supabase hands out `postgresql://…`; some tools emit the legacy `postgres://`. Both
        would otherwise pick a sync/unavailable driver. Forgetting `sslmode=require` against
        Supabase is a classic twenty-minute loss on demo day, so add it here rather than
        expecting whoever pastes the URL to remember.
        """
        v = re.sub(r"^postgres(ql)?://", "postgresql+psycopg://", v)
        if not v.startswith("postgresql+psycopg://"):
            raise ValueError(f"database_url must be a postgresql url, got: {v.split('://')[0]}")

        host_part = v.split("://", 1)[1].split("/", 1)[0]
        host = host_part.rsplit("@", 1)[-1].split(":", 1)[0]
        if host not in LOCAL_HOSTS and "sslmode=" not in v:
            v += ("&" if "?" in v else "?") + "sslmode=require"
        return v

    @field_validator("sms_sender_id")
    @classmethod
    def _check_sender_id(cls, v: str) -> str:
        # 46elks alphanumeric sender IDs are capped at 11 characters; a longer one is silently
        # rejected by the carrier, which surfaces as "SMS just never arrived" mid-demo.
        if len(v) > 11:
            raise ValueError(f"sms_sender_id must be <= 11 chars for 46elks, got {len(v)}")
        return v

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.dashboard_origins.split(",") if o.strip()]

    @property
    def sqlalchemy_url(self) -> str:
        return self.database_url

    @property
    def alembic_url(self) -> str:
        return self.database_url

    @property
    def safe_database_url(self) -> str:
        """The URL with the password masked. This is the only form safe to log."""
        return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", self.database_url)

    def url_for_database(self, name: str) -> str:
        """Same host and credentials, different database. Used to build the test DB URL."""
        base, _, tail = self.database_url.rpartition("/")
        query = f"?{tail.split('?', 1)[1]}" if "?" in tail else ""
        return f"{base}/{name}{query}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
