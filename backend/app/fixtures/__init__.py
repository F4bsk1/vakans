"""Bundled demo data.

`demo_clinic_profile.json` is the step-1 input: the seed generator needs a `clinic_profile`
before the ingestion pipeline exists. It is spec-shaped so it loads through exactly the same
code path as a real ingestion artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas.clinic_profile import ClinicProfile, ClinicProfileDocument, ProfileMeta

FIXTURES_DIR = Path(__file__).parent
DEMO_PROFILE_PATH = FIXTURES_DIR / "demo_clinic_profile.json"


def load_demo_profile() -> ClinicProfile:
    data = json.loads(DEMO_PROFILE_PATH.read_text(encoding="utf-8"))
    return ClinicProfile.from_spec_json(data)


def load_demo_profile_document() -> ClinicProfileDocument:
    """The demo profile wrapped for storage in `clinics.profile`.

    `source="hardcoded"` matters: `/sim/reset` only enforces the human-review gate for
    profiles that came from ingestion, so the bundled demo clinic seeds without ceremony
    while a scraped real clinic cannot.
    """
    return ClinicProfileDocument(
        profile=load_demo_profile(),
        meta=ProfileMeta(source="hardcoded", extractor=None),
    )
