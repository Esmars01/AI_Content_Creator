"""Meta edits reach the render (audit OUT-ASPECT): `set_meta` used to change `meta` only, while the
graph renders `render.outputs` — "make it 16:9" rendered the 9:16 preset again with nothing rebuilt."""

from __future__ import annotations

from ce_testing.edits import propose_ops
from ce_testing.fixtures import example_spec_dict


def test_a_new_aspect_renders_the_platform_preset_in_that_aspect() -> None:
    data = example_spec_dict()
    assert data["render"]["outputs"] == [{"preset_id": "tiktok_1080x1920_30", "aspect": "9:16"}]
    p = propose_ops(data, [{"op": "set_meta", "platform_targets": ["youtube"], "primary_aspect": "16:9"}])
    assert p.status == "proposed", p.issues
    assert p.spec is not None
    assert [(o.preset_id, o.aspect) for o in p.spec.render.outputs] == [("youtube_1920x1080_30", "16:9")]
    assert any(k.startswith("render.final:youtube_1920x1080_30") for k in p.impact["regenerate"]), p.impact


def test_an_aspect_no_target_platform_renders_falls_back_to_another_platform() -> None:
    p = propose_ops(example_spec_dict(), [{"op": "set_meta", "primary_aspect": "1:1"}])
    assert p.status == "proposed", p.issues
    assert p.spec is not None and [o.aspect for o in p.spec.render.outputs] == ["1:1"]


def test_meta_edits_without_platform_or_aspect_keep_the_outputs() -> None:
    p = propose_ops(example_spec_dict(), [{"op": "set_meta", "title": "A new title"}])
    assert p.status == "proposed", p.issues
    assert p.spec is not None
    assert [(o.preset_id, o.aspect) for o in p.spec.render.outputs] == [("tiktok_1080x1920_30", "9:16")]
