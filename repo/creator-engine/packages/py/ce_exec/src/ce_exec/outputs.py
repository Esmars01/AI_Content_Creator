"""Node outputs (§12.2): every node produces one primary artifact, a canonical JSON document.

`NodeOutput` holds the node's data (alignment words, compiled directives, measurements…) and
references to the media it produced or selected (`refs`, by sha256). Its sha256 is the node's
output hash: downstream cache keys use it, the cache maps a key to it, and the BuildManifest's
artifact map points at its `artifacts` row. Media referenced from it are registered as artifacts
too, so garbage collection keeps them while the document is referenced.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from ce_contracts.common import ArtifactRef
from ce_core.canonical import canonical_json
from ce_storage.content import ContentStore
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["DOC_MIME", "OUTPUT_KINDS", "DocCache", "NodeOutput", "output_kind"]

DOC_MIME = "application/json"

# Artifact kind of each node kind's output document (closed set, §12.2); media keep their own kinds.
OUTPUT_KINDS: dict[str, str] = {
    "behavior.resolve": "cbs",
    "behavior.compile_voice": "compiled_behavior",
    "behavior.compile_visual": "compiled_behavior",
    "behavior.keyframe_state": "keyframe_state",
    "behavior.observe": "observed_behavior",
    "behavior.coverage": "coverage_report",
    "align.segment": "alignment",
    "voice.prepare": "voice_conditioning",
    "captions.build": "captions",
    "captions.translate": "captions",
}


def output_kind(node_kind: str) -> str:
    return OUTPUT_KINDS.get(node_kind, "other")


class NodeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    v: int = 1
    node_kind: str
    data: dict[str, Any] = Field(default_factory=dict)
    refs: dict[str, ArtifactRef] = Field(default_factory=dict)

    def ref(self, role: str) -> ArtifactRef:
        try:
            return self.refs[role]
        except KeyError:
            raise KeyError(f"{self.node_kind} output has no {role!r} reference") from None

    def encode(self) -> bytes:
        refs = {k: v.model_dump(mode="json", exclude={"artifact_id"}) for k, v in self.refs.items()}
        return canonical_json({"v": self.v, "node_kind": self.node_kind, "data": self.data, "refs": refs}).encode(
            "utf-8"
        )

    @classmethod
    def decode(cls, raw: bytes) -> NodeOutput:
        return cls.model_validate_json(raw)


class DocCache:
    """In-process LRU of decoded JSON documents (outputs, graphs, requests) by sha256."""

    def __init__(self, store: ContentStore, *, capacity: int = 4096) -> None:
        self.store = store
        self.capacity = capacity
        self._items: OrderedDict[str, Any] = OrderedDict()

    async def raw(self, sha256: str) -> bytes:
        cached = self._items.get(sha256)
        if isinstance(cached, bytes):
            self._items.move_to_end(sha256)
            return cached
        data = await self.store.read_bytes(sha256)
        self._put(sha256, data)
        return data

    def _put(self, key: str, value: Any) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)

    async def output(self, sha256: str) -> NodeOutput:
        key = f"out:{sha256}"
        cached = self._items.get(key)
        if cached is None:
            cached = NodeOutput.decode(await self.store.read_bytes(sha256))
            self._put(key, cached)
        return cached  # type: ignore[no-any-return]

    async def put_output(self, output: NodeOutput) -> str:
        sha = await self.store.put_bytes(output.encode(), mime=DOC_MIME)
        self._put(f"out:{sha}", output)
        return sha

    async def put_json(self, value: Any) -> str:
        raw = canonical_json(value).encode("utf-8")
        sha = await self.store.put_bytes(raw, mime=DOC_MIME)
        self._put(sha, raw)
        return sha
