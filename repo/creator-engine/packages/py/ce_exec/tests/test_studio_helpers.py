"""Studio helpers (Phase 10): every ArtifactRef of a request is a worker input; plate fingerprints
order warm and cool light and see a key light's side."""

from __future__ import annotations

from pathlib import Path

from ce_exec.studio import STAGES, refs_in, run_stage  # noqa: F401
from ce_exec.studio_jobs import cosine
from ce_world.plates import plate_statistics
from PIL import Image

SHA = "a" * 64


def test_refs_in_finds_nested_refs_only() -> None:
    request = {
        "image": {"sha256": SHA, "kind": "image", "mime": "image/png"},
        "references": [{"sha256": "b" * 64, "kind": "image"}, {"sha256": "short", "kind": "image"}],
        "labels": {"sha256": "not a ref"},
    }
    assert [r.sha256 for r in refs_in(request)] == [SHA, "b" * 64]


def test_plate_statistics_order_colour_temperature_and_key_side(tmp_path: Path) -> None:
    warm, cool, left_lit = tmp_path / "warm.png", tmp_path / "cool.png", tmp_path / "left.png"
    Image.new("RGB", (64, 64), (255, 170, 90)).save(warm)
    Image.new("RGB", (64, 64), (150, 190, 255)).save(cool)
    image = Image.new("RGB", (64, 64), (40, 40, 40))
    image.paste((230, 230, 230), (0, 0, 32, 64))
    image.save(left_lit)
    w, c, lft = plate_statistics(warm), plate_statistics(cool), plate_statistics(left_lit)
    assert w["cct_k"] < c["cct_k"]
    assert lft["left_right_ratio"] > 5 and 0 < lft["luminance"] < 1
    assert "reliability low" in w["method"]


def test_every_studio_kind_has_a_start_stage() -> None:
    # the same modules `ce_exec.studio.run_stage` imports, so the result does not depend on test order
    import ce_exec.bench_job
    import ce_exec.consistency_job
    import ce_exec.creator_test
    import ce_exec.critique_job
    import ce_exec.memory_job
    import ce_exec.packaging_job
    import ce_exec.research_job
    import ce_exec.studio_jobs  # noqa: F401

    kinds = {kind for kind, _ in STAGES}
    assert kinds == {
        "identity_pack", "wardrobe_refs", "voice_design", "voice_test", "world_plates", "creator_test",
        "consistency", "critique", "autonomous_suggest", "benchmark", "memory_update", "research_ingest",
        "package",
    }  # fmt: skip
    assert all((kind, "start") in STAGES for kind in kinds)
    assert cosine([1.0, 2.0], [2.0, 4.0]) == 1.0
