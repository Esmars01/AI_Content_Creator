"""YAML loading with YAML 1.2 booleans.

PyYAML implements YAML 1.1, where `on`, `off`, `yes`, `no`, `y` and `n` are booleans. Element
states such as `on`/`off` (§19) must stay strings, so every config and vocabulary file is
loaded with a SafeLoader whose only booleans are `true` and `false` (any case).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

__all__ = ["load_yaml_file", "safe_load"]


class _Loader(yaml.SafeLoader):
    pass


# Drop the YAML 1.1 bool resolver and register a YAML 1.2 one.
_Loader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def safe_load(text: str) -> Any:
    return yaml.load(text, Loader=_Loader)  # noqa: S506 - _Loader derives from SafeLoader


def load_yaml_file(path: Path | str) -> Any:
    return safe_load(Path(path).read_text(encoding="utf-8"))
