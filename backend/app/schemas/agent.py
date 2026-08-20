"""Request/response models for the agent tool endpoints (spec §3).

These double as the JSON schemas handed to the voice platform's function-calling config, so
**keep the parameter lists short**. Spec §4: "Håll parametrarna få — LLM:en klantar sig med
många." Every optional field is one more thing the model can get wrong mid-call.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ToolResponse(BaseModel):
    """What every agent tool returns. The agent reads `speech`; the dashboard gets `data`."""

    speech: str = Field(description="Read this aloud, verbatim. Already phrased in Swedish.")
    data: dict[str, Any] = Field(default_factory=dict)


class AvailabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_name: str | None = Field(
        default=None, description="Behandling som kunden bad om, som de sa det."
    )
    resource_name: str | None = Field(
        default=None, description="Behandlare som kunden bad om, om någon."
    )
    earliest: datetime | None = None
    latest: datetime | None = None


class BookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_id: UUID
    service_id: UUID
    patient_name: str | None = None
    phone: str | None = None


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    booking_ref: str | None = None
    phone: str | None = None
    name: str | None = None


class RescheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_slot_id: UUID
    booking_ref: str | None = None
    phone: str | None = None
    name: str | None = None


class WaitlistRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: UUID
    patient_name: str | None = None
    phone: str | None = None
    earliest: datetime | None = None
    latest: datetime | None = None


class KnowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str


class EscalateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None
    phone: str | None = None
    name: str | None = None


class AgentConfigResponse(BaseModel):
    """Everything the voice platform needs to run this clinic's agent (step 4)."""

    clinic_id: UUID
    slug: str
    clinic_name: str
    timezone: str
    demo_phone: str | None
    greeting: str
    system_prompt: str
