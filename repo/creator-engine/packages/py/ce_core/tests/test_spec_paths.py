from __future__ import annotations

import pytest
from ce_core.spec.paths import SpecPath, SpecPathError

DOC = {
    "cast": [{"key": "char_alex", "overrides": {"voice_version_id": None}}],
    "meta": {"platform_targets": ["tiktok", "youtube_shorts"]},
    "memory": {"snapshots": [{"character_key": "char_alex", "snapshot_id": "s1"}]},
    "scenes": [
        {
            "key": "scn_hook",
            "acting": {
                "states": [
                    {"key": "st_1", "emotion": {"displayed": "confident"}},
                    {"key": "st_2", "emotion": {"displayed": "serious"}},
                ]
            },
            "world": {"overrides": {"element_states": {"el_monitor": "on"}}},
        },
        {"key": "scn_body", "acting": {"states": [{"key": "st_3", "emotion": {"displayed": "curious"}}]}},
    ],
    "generation": {"seed_overrides": {"avatar.render:sht_1:c2:t1": 42}},
    "render": {"outputs": [{"preset_id": "tiktok_1080x1920_30", "aspect": "9:16"}]},
}


@pytest.mark.parametrize(
    "text",
    [
        "/cast[char_alex]/overrides/voice_version_id",
        "/scenes[scn_hook]/acting/states[st_2]/emotion/displayed",
        "/generation/seed_overrides[avatar.render:sht_1:c2:t1]",
        "/",
    ],
)
def test_parse_round_trip(text: str) -> None:
    assert str(SpecPath.parse(text)) == text


@pytest.mark.parametrize(
    "bad", ["scenes", "/scenes[0]", "/scenes[]", "/Scenes", "/scenes[scn_a]/", "/scenes//x", "/scenes[a[b]]"]
)
def test_malformed_paths_are_rejected(bad: str) -> None:
    with pytest.raises(SpecPathError):
        SpecPath.parse(bad)


def test_glob_only_in_patterns() -> None:
    with pytest.raises(SpecPathError, match="only allowed"):
        SpecPath.parse("/script/segments[*]/text")
    assert SpecPath.parse("/script/segments[*]/text", allow_glob=True).is_glob


def test_resolve_by_key_character_key_preset_value_and_map() -> None:
    assert SpecPath.parse("/scenes[scn_hook]/acting/states[st_2]/emotion/displayed").resolve(DOC) == "serious"
    assert SpecPath.parse("/memory/snapshots[char_alex]/snapshot_id").resolve(DOC) == "s1"
    assert SpecPath.parse("/render/outputs[tiktok_1080x1920_30]/aspect").resolve(DOC) == "9:16"
    assert SpecPath.parse("/meta/platform_targets[youtube_shorts]").resolve(DOC) == "youtube_shorts"
    assert SpecPath.parse("/generation/seed_overrides[avatar.render:sht_1:c2:t1]").resolve(DOC) == 42
    assert SpecPath.parse("/scenes[scn_hook]/world/overrides/element_states[el_monitor]").resolve(DOC) == "on"


def test_resolve_missing_raises() -> None:
    assert not SpecPath.parse("/scenes[scn_missing]/acting").exists(DOC)
    with pytest.raises(SpecPathError, match="no element"):
        SpecPath.parse("/scenes[scn_missing]/acting").resolve(DOC)
    with pytest.raises(SpecPathError, match="not found"):
        SpecPath.parse("/scenes[scn_hook]/nope").resolve(DOC)


def test_expand_glob() -> None:
    pattern = SpecPath.parse("/scenes[*]/acting/states[*]/emotion", allow_glob=True)
    assert [str(p) for p in pattern.expand(DOC)] == [
        "/scenes[scn_hook]/acting/states[st_1]/emotion",
        "/scenes[scn_hook]/acting/states[st_2]/emotion",
        "/scenes[scn_body]/acting/states[st_3]/emotion",
    ]


def test_matches_covers_overlaps() -> None:
    pattern = SpecPath.parse("/script/segments[*]/text", allow_glob=True)
    assert pattern.matches(SpecPath.parse("/script/segments[seg_1]/text"))
    assert not pattern.matches(SpecPath.parse("/script/segments[seg_1]/annotations"))
    assert pattern.covers(SpecPath.parse("/script/segments[seg_1]/text"))
    assert not pattern.covers(SpecPath.parse("/script/segments"))
    # A change to the whole segment list (or one segment) overlaps the locked text.
    assert pattern.overlaps(SpecPath.parse("/script/segments"))
    assert pattern.overlaps(SpecPath.parse("/script/segments[seg_2]"))
    assert not pattern.overlaps(SpecPath.parse("/script/wording"))
    scoped = SpecPath.parse("/scenes[scn_hook]/world", allow_glob=True)
    assert scoped.overlaps(SpecPath.parse("/scenes[scn_hook]/world/time_of_day"))
    assert not scoped.overlaps(SpecPath.parse("/scenes[scn_body]/world/time_of_day"))


def test_json_pointer_is_display_only_with_indices() -> None:
    path = SpecPath.parse("/scenes[scn_hook]/acting/states[st_2]/emotion/displayed")
    assert path.to_json_pointer(DOC) == "/scenes/0/acting/states/1/emotion/displayed"
    assert SpecPath.parse("/generation/seed_overrides[avatar.render:sht_1:c2:t1]").to_json_pointer(DOC) == (
        "/generation/seed_overrides/avatar.render:sht_1:c2:t1"
    )


def test_parent_and_child() -> None:
    path = SpecPath.parse("/scenes[scn_hook]/acting")
    assert str(path.child("states", "st_1")) == "/scenes[scn_hook]/acting/states[st_1]"
    assert str(path.parent) == "/scenes[scn_hook]"
    with pytest.raises(SpecPathError):
        _ = SpecPath.parse("/").parent
