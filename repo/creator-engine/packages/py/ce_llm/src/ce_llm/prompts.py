"""Versioned prompt templates: `prompts/<stage>/<version>.md` (Jinja2, §13).

Each run records the template version it used. `prompts/system/<version>.md` is the shared system
preamble; the data rule (I10) is always appended to it. Templates render with `StrictUndefined`, so
a missing variable is an error, and untrusted text goes through the `data` filter:

    {{ memory_text | data("memory", item_id) }}
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jinja2
from ce_contracts.models import LLMMessage

from ce_llm.data import DATA_RULE, wrap_data

__all__ = ["PromptLibrary", "RenderedPrompt", "prompts_root"]

_VERSION = re.compile(r"^v(\d+)$")


@dataclass(frozen=True)
class RenderedPrompt:
    stage: str
    version: str
    system: str
    user: str

    def messages(self) -> list[LLMMessage]:
        return [LLMMessage(role="system", content=self.system), LLMMessage(role="user", content=self.user)]

    @property
    def template_version(self) -> str:
        return f"{self.stage}/{self.version}"


class PromptLibrary:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(self.root)),
            undefined=jinja2.StrictUndefined,
            autoescape=False,  # noqa: S701 - prompts are plain text for a model, not HTML
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.env.filters["data"] = lambda text, kind, source_id="": wrap_data(
            str(text), kind=str(kind), source_id=str(source_id)
        )

    def versions(self, stage: str) -> list[str]:
        folder = self.root / stage
        found = [p.stem for p in folder.glob("v*.md") if _VERSION.match(p.stem)] if folder.is_dir() else []
        return sorted(found, key=lambda v: int(v[1:]))

    def latest(self, stage: str) -> str:
        versions = self.versions(stage)
        if not versions:
            raise FileNotFoundError(f"no prompt template for stage {stage!r} under {self.root}")
        return versions[-1]

    def render(self, stage: str, version: str | None = None, **context: Any) -> RenderedPrompt:
        version = version or self.latest(stage)
        user = self.env.get_template(f"{stage}/{version}.md").render(**context).strip()
        system_version = self.latest("system")
        system = self.env.get_template(f"system/{system_version}.md").render(**context).strip()
        return RenderedPrompt(stage=stage, version=version, system=f"{system}\n\n{DATA_RULE}", user=user)


def prompts_root() -> Path:
    """`PROMPTS_DIR`, else `prompts/` next to the config root (images and checkouts)."""
    env = os.environ.get("PROMPTS_DIR")
    if env:
        return Path(env)
    candidates = []
    config_root = os.environ.get("CE_CONFIG_ROOT")
    if config_root:
        candidates.append(Path(config_root).resolve().parent / "prompts")
    candidates += [parent / "prompts" for parent in Path(__file__).resolve().parents]
    candidates.append(Path.cwd() / "prompts")
    for candidate in candidates:
        if (candidate / "system").is_dir():
            return candidate
    return candidates[-1]
