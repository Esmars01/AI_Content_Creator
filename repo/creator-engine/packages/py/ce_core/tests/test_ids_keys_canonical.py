from __future__ import annotations

import math
import time
import uuid

import pytest
from ce_core.canonical import canonical_json, content_digest
from ce_core.ids import new_id, uuid7, uuid7_unix_ms
from ce_core.keys import KeyKind, new_key, take_key, validate_key
from hypothesis import given
from hypothesis import strategies as st


def test_uuid7_version_variant_and_time() -> None:
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000
    assert value.version == 7
    assert value.variant == uuid.RFC_4122
    assert before <= uuid7_unix_ms(value) <= after + 1


def test_uuid7_is_strictly_monotonic_in_process() -> None:
    ids = [new_id() for _ in range(5000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_uuid7_unix_ms_rejects_other_versions() -> None:
    with pytest.raises(ValueError, match="not a UUIDv7"):
        uuid7_unix_ms(uuid.uuid4())


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (KeyKind.SCENE, "scn_hook"),
        (KeyKind.CHARACTER, "char_alex"),
        (KeyKind.ZONE, "zone_desk_chair"),
        (KeyKind.ELEMENT, "el_x_lamp"),
        (KeyKind.TAKE, "tk_2"),
    ],
)
def test_valid_keys(kind: KeyKind, value: str) -> None:
    assert validate_key(kind, value) == value


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (KeyKind.SCENE, "sht_1"),
        (KeyKind.SCENE, "scn_"),
        (KeyKind.SCENE, "scn_Hook"),
        (KeyKind.SCENE, "scn__a"),
        (KeyKind.SCENE, "scn_" + "a" * 70),
    ],
)
def test_invalid_keys(kind: KeyKind, value: str) -> None:
    with pytest.raises(ValueError, match="invalid"):
        validate_key(kind, value)


def test_new_key_never_reuses() -> None:
    assert new_key(KeyKind.SEGMENT) == "seg_1"
    assert new_key(KeyKind.SEGMENT, ["seg_1", "seg_3", "seg_intro"]) == "seg_4"
    assert take_key(2) == "tk_2"
    with pytest.raises(ValueError):
        take_key(0)


def test_canonical_json_is_order_and_float_normalized() -> None:
    a = {"b": 1.0, "a": [0.5, -0.0, 2], "c": {"z": True, "y": None}}
    b = {"c": {"y": None, "z": True}, "a": [0.5, 0.0, 2.0], "b": 1}
    assert canonical_json(a) == canonical_json(b) == '{"a":[0.5,0,2],"b":1,"c":{"y":null,"z":true}}'
    assert content_digest(a) == content_digest(b)
    assert content_digest(a).startswith("sha256:")


def test_canonical_json_keeps_unicode_unescaped() -> None:
    assert canonical_json({"t": "İstanbul"}) == '{"t":"İstanbul"}'


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_canonical_json_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError):
        canonical_json({"x": bad})


def test_canonical_json_rejects_sets_and_non_string_keys() -> None:
    with pytest.raises(TypeError):
        canonical_json({"x": {1, 2}})
    with pytest.raises(TypeError):
        canonical_json({1: "x"})


@given(st.dictionaries(st.text(max_size=5), st.floats(allow_nan=False, allow_infinity=False), max_size=6))
def test_digest_is_stable_under_reordering(d: dict[str, float]) -> None:
    reordered = dict(reversed(list(d.items())))
    assert content_digest(d) == content_digest(reordered)
