"""The project's brand kit is the default brand of a new plan (Phase 12, ADR 0059): the kit id,
the logo overlay when the kit has a logo, and the kit's caption style unless the request names
one."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from typing import Any

import pytest
from ce_build.refs import BrandKitRef
from ce_director import BrandDefault, Director, PlanRequest
from ce_testing.director import director_context, director_deps, fixture_inputs, refs_for

pytestmark = [pytest.mark.behavior]

KIT = uuid.UUID(int=4242)


async def _refs(spec: Any, snapshots: Any) -> Any:
    refs = await refs_for(spec, snapshots)
    refs.brand_kits[KIT] = BrandKitRef(KIT, {"primary": "#000000"}, {}, "clean_subtitle", None)
    return refs


def _plan(brand: BrandDefault | None, **request: Any) -> Any:
    ctx = dataclasses.replace(director_context(), brand=brand)
    deps = dataclasses.replace(director_deps(), refs_for=_refs)
    return asyncio.run(Director(deps).plan(PlanRequest(input=fixture_inputs("explain_ai_agents")[0], **request), ctx))


def test_a_new_plan_takes_the_project_brand_kit() -> None:
    outcome = _plan(BrandDefault(KIT, logo_overlay=True, caption_style_id="clean_subtitle"))
    assert outcome.spec.brand.brand_kit_id == KIT and outcome.spec.brand.logo_overlay is True
    assert outcome.spec.captions.style_id == "clean_subtitle"


def test_the_request_caption_style_wins_and_no_kit_means_no_brand() -> None:
    chosen = _plan(BrandDefault(KIT, caption_style_id="clean_subtitle"), caption_style_id="karaoke_box")
    assert chosen.spec.captions.style_id == "karaoke_box" and chosen.spec.brand.logo_overlay is False
    plain = _plan(None)
    assert plain.spec.brand.brand_kit_id is None and plain.spec.brand.logo_overlay is False
