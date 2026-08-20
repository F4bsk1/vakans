"""Agent tool endpoints — spec §3.

A thin HTTP shell over `app.agent.tools`. The voice platform (Retell in step 4, per charter
D1) calls these as function tools and reads back the `speech` field verbatim.

Keeping this layer thin is the whole point of D1: everything that matters lives below it, so
swapping Retell for Pipecat later touches nothing here or beneath.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import prompt, tools
from app.config import Settings, get_settings
from app.db import get_session
from app.models import Clinic
from app.schemas.agent import (
    AgentConfigResponse,
    AvailabilityRequest,
    BookRequest,
    CancelRequest,
    EscalateRequest,
    KnowledgeRequest,
    RescheduleRequest,
    ToolResponse,
    WaitlistRequest,
)
from app.schemas.clinic_profile import ClinicProfile
from app.timeutils import now_utc

router = APIRouter(prefix="/agent", tags=["agent"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def _clinic(session: AsyncSession, clinic_id: UUID) -> Clinic:
    clinic = (
        await session.execute(select(Clinic).where(Clinic.id == clinic_id))
    ).scalar_one_or_none()
    if clinic is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="clinic not found")
    return clinic


@router.get("/{clinic_id}/config", response_model=AgentConfigResponse)
async def agent_config(clinic_id: UUID, session: SessionDep) -> AgentConfigResponse:
    """The per-clinic prompt and greeting, for configuring the voice agent (step 4)."""
    clinic = await _clinic(session, clinic_id)
    profile = ClinicProfile.model_validate(clinic.profile["profile"])
    return AgentConfigResponse(
        clinic_id=clinic.id,
        slug=clinic.slug,
        clinic_name=clinic.name,
        timezone=clinic.timezone,
        demo_phone=clinic.demo_phone,
        greeting=prompt.build_greeting(profile),
        system_prompt=prompt.build_system_prompt(profile),
    )


@router.post("/{clinic_id}/availability", response_model=ToolResponse)
async def availability(
    clinic_id: UUID, body: AvailabilityRequest, session: SessionDep
) -> ToolResponse:
    clinic = await _clinic(session, clinic_id)
    result = await tools.availability(
        session,
        clinic,
        service_name=body.service_name,
        resource_name=body.resource_name,
        earliest=body.earliest,
        latest=body.latest,
        now=now_utc(),
    )
    await session.commit()
    return ToolResponse(**result.as_dict())


@router.post("/{clinic_id}/book", response_model=ToolResponse)
async def book(
    clinic_id: UUID, body: BookRequest, session: SessionDep, settings: SettingsDep
) -> ToolResponse:
    clinic = await _clinic(session, clinic_id)
    result = await tools.book(
        session,
        clinic,
        slot_id=body.slot_id,
        service_id=body.service_id,
        patient_name=body.patient_name,
        phone=body.phone,
        settings=settings,
        now=now_utc(),
    )
    await session.commit()
    return ToolResponse(**result.as_dict())


@router.post("/{clinic_id}/reschedule", response_model=ToolResponse)
async def reschedule(
    clinic_id: UUID, body: RescheduleRequest, session: SessionDep, settings: SettingsDep
) -> ToolResponse:
    clinic = await _clinic(session, clinic_id)
    result = await tools.reschedule(
        session,
        clinic,
        new_slot_id=body.new_slot_id,
        booking_ref=body.booking_ref,
        phone=body.phone,
        name=body.name,
        settings=settings,
        now=now_utc(),
    )
    await session.commit()
    return ToolResponse(**result.as_dict())


@router.post("/{clinic_id}/cancel", response_model=ToolResponse)
async def cancel(clinic_id: UUID, body: CancelRequest, session: SessionDep) -> ToolResponse:
    """Cancelling emits `slot_freed`, which is what the step-3b refill cascade hangs off."""
    clinic = await _clinic(session, clinic_id)
    result = await tools.cancel(
        session,
        clinic,
        booking_ref=body.booking_ref,
        phone=body.phone,
        name=body.name,
        now=now_utc(),
    )
    await session.commit()
    return ToolResponse(**result.as_dict())


@router.post("/{clinic_id}/waitlist", response_model=ToolResponse)
async def waitlist(clinic_id: UUID, body: WaitlistRequest, session: SessionDep) -> ToolResponse:
    clinic = await _clinic(session, clinic_id)
    result = await tools.add_to_waitlist(
        session,
        clinic,
        service_id=body.service_id,
        patient_name=body.patient_name,
        phone=body.phone,
        earliest=body.earliest,
        latest=body.latest,
        now=now_utc(),
    )
    await session.commit()
    return ToolResponse(**result.as_dict())


@router.post("/{clinic_id}/knowledge", response_model=ToolResponse)
async def knowledge(clinic_id: UUID, body: KnowledgeRequest, session: SessionDep) -> ToolResponse:
    clinic = await _clinic(session, clinic_id)
    result = await tools.knowledge(session, clinic, question=body.question, now=now_utc())
    await session.commit()
    return ToolResponse(**result.as_dict())


@router.post("/{clinic_id}/escalate", response_model=ToolResponse)
async def escalate(clinic_id: UUID, body: EscalateRequest, session: SessionDep) -> ToolResponse:
    clinic = await _clinic(session, clinic_id)
    result = await tools.escalate(
        session,
        clinic,
        reason=body.reason,
        phone=body.phone,
        name=body.name,
        now=now_utc(),
    )
    await session.commit()
    return ToolResponse(**result.as_dict())
