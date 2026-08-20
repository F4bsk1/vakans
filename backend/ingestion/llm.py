"""Provider-agnostic structured extraction.

Charter decision D2: keep the model behind an abstraction so switching provider is a config
change. Azure OpenAI Sweden Central needs subscription approval that can take days, and the
fallbacks are Bedrock eu-north-1 or Mistral — all EU, all one env var apart.

Two deliberate choices:

1. **Pydantic AI rather than `instructor`**, a documented deviation from charter §3. Pydantic
   AI already does Pydantic-validated structured output with retries and already gives us the
   provider-agnosticism D2 demands. Running both would mean two provider switches, two
   credential paths and two fake-modes. Reversible: implement `ProfileExtractor` and swap.

2. **`fake` is the default provider.** The entire test suite and the whole step-1 seed flow run
   with no credentials and no network. A test suite that needs an API key is a test suite that
   stops being run.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.config import Settings

logger = logging.getLogger("vakans.ingestion.llm")


@dataclass(frozen=True)
class Chunk:
    """A batch of page text handed to the model in one call."""

    index: int
    urls: list[str]
    text: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()[:16]


@dataclass
class ExtractionUsage:
    """Rough cost accounting. You will want "cost per clinic demo" for the pitch."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens


class ProfileExtractor(Protocol):
    async def extract(self, chunk: Chunk) -> object: ...

    @property
    def name(self) -> str: ...


def build_model(settings: Settings) -> object:
    """Return a Pydantic AI model for the configured provider.

    Imports are local so that `provider=fake` never pulls in a cloud SDK.
    """
    provider = settings.llm_provider

    if provider == "fake":
        from pydantic_ai.models.test import TestModel

        return TestModel()

    if provider == "azure":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.azure import AzureProvider

        if not (settings.azure_openai_endpoint and settings.azure_openai_api_key):
            raise RuntimeError(
                "VAKANS_LLM_PROVIDER=azure needs VAKANS_AZURE_OPENAI_ENDPOINT and "
                "VAKANS_AZURE_OPENAI_API_KEY. Use provider=fake to run without credentials."
            )
        return OpenAIChatModel(
            settings.llm_model,
            provider=AzureProvider(
                azure_endpoint=settings.azure_openai_endpoint,
                api_key=settings.azure_openai_api_key.get_secret_value(),
                api_version=settings.azure_openai_api_version,
            ),
        )

    if provider == "openai":
        from pydantic_ai.models.openai import OpenAIChatModel

        if not settings.openai_api_key:
            raise RuntimeError("VAKANS_LLM_PROVIDER=openai needs VAKANS_OPENAI_API_KEY")
        return OpenAIChatModel(settings.llm_model)

    if provider == "bedrock":
        from pydantic_ai.models.bedrock import BedrockConverseModel

        return BedrockConverseModel(settings.llm_model)

    if provider == "mistral":
        from pydantic_ai.models.mistral import MistralModel

        if not settings.mistral_api_key:
            raise RuntimeError("VAKANS_LLM_PROVIDER=mistral needs VAKANS_MISTRAL_API_KEY")
        return MistralModel(settings.llm_model, api_key=settings.mistral_api_key.get_secret_value())

    raise RuntimeError(f"unknown llm_provider: {provider}")


class PydanticAIExtractor:
    """Structured extraction with automatic retries on validation failure.

    `output_type` is the `PartialProfile` from extract.py, which is `extra="forbid"` — so an
    invented key becomes a validation error the model is asked to correct, rather than data
    silently dropped.
    """

    def __init__(self, settings: Settings, output_type: type, system_prompt: str) -> None:
        from pydantic_ai import Agent

        self._settings = settings
        self._model = build_model(settings)
        self._agent = Agent(
            self._model,
            output_type=output_type,
            system_prompt=system_prompt,
            retries=3,
        )
        self.usage = ExtractionUsage()

    @property
    def name(self) -> str:
        return f"pydantic-ai:{self._settings.llm_provider}:{self._settings.llm_model}"

    async def extract(self, chunk: Chunk) -> object:
        result = await self._agent.run(chunk.text)
        try:
            usage = result.usage()
            self.usage.add(
                getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0
            )
        except Exception:  # usage accounting must never break an extraction
            logger.debug("could not read usage from model result")
        return result.output


class FixtureExtractor:
    """Replay recorded model output, keyed on chunk digest.

    This is what lets merge.py and validate.py be tested against *genuine* model output —
    including its mistakes — rather than idealised hand-written JSON.
    """

    def __init__(self, responses: dict[str, dict], output_type: type, *, strict: bool = False):
        self._responses = responses
        self._output_type = output_type
        self._strict = strict

    @classmethod
    def from_file(cls, path: Path, output_type: type, **kw) -> FixtureExtractor:
        return cls(json.loads(path.read_text(encoding="utf-8")), output_type, **kw)

    @property
    def name(self) -> str:
        return "fixture"

    async def extract(self, chunk: Chunk) -> object:
        payload = self._responses.get(chunk.digest)
        if payload is None:
            payload = self._responses.get(str(chunk.index))
        if payload is None:
            if self._strict:
                raise KeyError(f"no recorded response for chunk {chunk.index} ({chunk.digest})")
            return self._output_type()
        return self._output_type.model_validate(payload)


class RecordingExtractor:
    """Wrap a real extractor and save what it returns, to build fixtures from one live run."""

    def __init__(self, inner: ProfileExtractor, path: Path) -> None:
        self._inner = inner
        self._path = path
        self._recorded: dict[str, dict] = {}

    @property
    def name(self) -> str:
        return f"recording({self._inner.name})"

    async def extract(self, chunk: Chunk) -> object:
        output = await self._inner.extract(chunk)
        self._recorded[chunk.digest] = json.loads(output.model_dump_json())
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._recorded, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return output
