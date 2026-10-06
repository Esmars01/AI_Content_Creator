"""How a build graph is split for GenerateVersionWorkflow (`ce_exec.runtime._partition`)."""

from __future__ import annotations

from ce_build import BuildOptions, build_graph
from ce_exec.runtime import _partition
from ce_testing.build import config_bundle, example_build_refs, mock_catalog
from ce_testing.fixtures import example_spec


def test_nodes_no_scene_needs_run_alongside_the_scenes_not_before_them() -> None:
    """Audit P4: the example's SFX node has no dependencies and no scene depends on it, yet it was
    in `pre`, so every scene (TTS included) waited for it on the worker queue."""
    graph = build_graph(example_spec(), example_build_refs(), config_bundle(), mock_catalog(), options=BuildOptions())
    pre, scenes, post, side = _partition(graph)
    by_key = graph.by_key()
    in_scene = {k for keys in scenes.values() for k in keys}
    assert "audio.sfx:sfx_1" in side and "audio.sfx:sfx_1" not in pre
    # every pre node is needed by some scene node; every side node has no dependencies
    for key in pre:
        assert any(key in by_key[s].deps for s in in_scene), key
    assert all(not by_key[k].deps for k in side)
    # nothing is lost or duplicated, and post still holds what needs the side nodes (the mix)
    assert sorted(pre + post + side + sorted(in_scene)) == sorted(n.key for n in graph.nodes)
    assert any("audio.sfx:sfx_1" in by_key[k].deps for k in post)
