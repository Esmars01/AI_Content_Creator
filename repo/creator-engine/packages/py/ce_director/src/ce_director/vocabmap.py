"""Closed-vocabulary checks for LLM output (§13, I13).

LLM output is never trusted for control values. Each stage lists the control fields of its
output (`ControlField`: a dotted path into the parsed value and a vocabulary category). Unknown
labels go back to the model through the repair loop; a label still unknown after the repairs is
mapped to the nearest vocabulary item and the mapping is recorded as an assumption.

Nearest = an exact alias in `descriptions.yaml`, else the best lexical match (character-trigram
and word overlap between the label and each token, its aliases and its description), plus — since
Phase 12, when the Director embedded the label and the tokens' descriptions — their cosine
similarity (`semantic`), all behind the same function.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from ce_core.vocab import Vocabulary

__all__ = ["ControlField", "VocabMapper", "iter_values", "set_value"]


def _norm(label: str) -> str:
    return "_".join(str(label).strip().lower().replace("-", " ").split())


def _trigrams(text: str) -> set[str]:
    padded = f"  {text} "
    return {padded[i : i + 3] for i in range(len(padded) - 2)}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


@dataclass(frozen=True)
class ControlField:
    """`path` is dotted with `[]` for lists, e.g. `scenes[].states[].strategies.gaze`."""

    path: str
    category: str
    optional: bool = True


def iter_values(data: Any, path: str) -> Iterator[tuple[str, Any]]:
    """(concrete path, value) for every value at `path` (lists expanded with their index)."""
    parts = path.split(".")

    def walk(node: Any, i: int, prefix: str) -> Iterator[tuple[str, Any]]:
        if i == len(parts):
            yield prefix, node
            return
        part = parts[i]
        many = part.endswith("[]")
        name = part[:-2] if many else part
        child = node.get(name) if isinstance(node, dict) else getattr(node, name, None)
        here = f"{prefix}.{name}" if prefix else name
        if child is None:
            return
        if many:
            for k, item in enumerate(child):
                yield from walk(item, i + 1, f"{here}[{k}]")
        else:
            yield from walk(child, i + 1, here)

    yield from walk(data, 0, "")


def set_value(data: dict[str, Any], concrete: str, value: Any) -> None:
    """Sets a value addressed by a concrete path from `iter_values` (dicts only)."""
    node: Any = data
    tokens = concrete.replace("]", "").split(".")
    for t_index, token in enumerate(tokens):
        name, _, index = token.partition("[")
        last = t_index == len(tokens) - 1
        if index:
            node = node[name][int(index)]
            if last:
                raise ValueError("cannot set a list element directly")
        elif last:
            node[name] = value
        else:
            node = node[name]


class VocabMapper:
    def __init__(self, vocab: Vocabulary) -> None:
        self.vocab = vocab
        self._aliases: dict[str, dict[str, str]] = {}
        # (category, label) → token → cosine(label, token description); filled by the Director
        self.semantic: dict[tuple[str, str], dict[str, float]] = {}

    def token_text(self, category: str, token: str) -> str:
        """A token as text for embedding: its name, aliases and description."""
        description = self.vocab.describe(category, token)
        parts = [token.replace("_", " ")]
        if description is not None:
            parts += [*description.aliases, description.text]
        return "; ".join(p for p in parts if p)

    def unknown(self, data: Any, fields: list[ControlField]) -> list[tuple[str, str]]:
        """(category, label) of every unknown control value (deduplicated, in order)."""
        out: list[tuple[str, str]] = []
        for f in fields:
            for _, value in iter_values(data, f.path):
                if value is None and f.optional:
                    continue
                if not self.known(f.category, value) and (f.category, str(value)) not in out:
                    out.append((f.category, str(value)))
        return out

    def _alias_index(self, category: str) -> dict[str, str]:
        if category not in self._aliases:
            index: dict[str, str] = {}
            for token, description in self.vocab.descriptions.get(category, {}).items():
                for alias in description.aliases:
                    index.setdefault(_norm(alias), token)
            self._aliases[category] = index
        return self._aliases[category]

    def known(self, category: str, label: str | None) -> bool:
        return label is not None and self.vocab.has(category, str(label))

    def nearest(self, category: str, label: str) -> str:
        """The closest token of `category` to an unknown label (deterministic)."""
        tokens = sorted(self.vocab.tokens(category))
        normalized = _norm(label)
        if normalized in tokens:
            return normalized
        alias = self._alias_index(category).get(normalized)
        if alias is not None:
            return alias
        grams = _trigrams(normalized.replace("_", " "))
        words = set(normalized.split("_"))

        def score(token: str) -> float:
            description = self.vocab.describe(category, token)
            names = [token, *(description.aliases if description else [])]
            best = max(_jaccard(grams, _trigrams(n.replace("_", " "))) for n in names)
            text_words = set((description.text if description else "").lower().replace(",", " ").split())
            overlap = len(words & (text_words | set(token.split("_")))) / max(1, len(words))
            return best + 0.5 * overlap + hints.get(token, 0.0)

        hints = self.semantic.get((category, label), {})

        return max(tokens, key=score)  # ties: the alphabetically first token (tokens are sorted)

    def problems(self, data: Any, fields: list[ControlField]) -> list[str]:
        """Repair-loop problems: every unknown label with the allowed values."""
        out: list[str] = []
        for f in fields:
            for concrete, value in iter_values(data, f.path):
                if value is None and f.optional:
                    continue
                if not self.known(f.category, value):
                    allowed = ", ".join(sorted(self.vocab.tokens(f.category)))
                    out.append(f"{concrete}: {value!r} is not allowed; use one of: {allowed}")
        return out

    def coerce(
        self,
        data: dict[str, Any],
        fields: list[ControlField],
        note: Callable[[str], None],
        *,
        stage: str,
    ) -> dict[str, Any]:
        """Maps every remaining unknown label to its nearest token, noting each mapping."""
        for f in fields:
            for concrete, value in list(iter_values(data, f.path)):
                if value is None and f.optional:
                    continue
                if not self.known(f.category, value):
                    mapped = self.nearest(f.category, str(value))
                    set_value(data, concrete, mapped)
                    note(
                        f"{stage}: the model's {f.category} {value!r} at {concrete} is not in the vocabulary; "
                        f"mapped to the nearest item {mapped!r}"
                    )
        return data
