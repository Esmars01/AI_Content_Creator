"""I14 — Engines are adapters: core packages and services never import plugin packages or engine
libraries; they reach engines through the plugin registry, the router and the adapter contracts.
Plugins depend on the contracts, never on core services."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = [pytest.mark.invariant]

ROOT = Path(__file__).resolve().parents[2]

PLUGIN_PREFIXES = ("ce_plugin", "ce_plugins")
# Model and engine libraries live in plugins. (librosa/OpenCV/FFmpeg are media tools of
# ce_render and ce_camera, §7, §27; analyzers built on them still run as adapters.)
ENGINE_LIBRARIES = {
    "torch",
    "diffusers",
    "transformers",
    "mediapipe",
    "onnxruntime",
    "faster_whisper",
    "whisper",
    "vllm",
    "lightx2v",
    "paddleocr",
    "c2pa",
    "audioseal",
    "videoseal",
}
CORE_FORBIDDEN_IN_PLUGINS = {"ce_db", "ce_api", "ce_orchestrator", "ce_scheduler", "ce_exec", "ce_build", "ce_router"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


# The plugin SDK (ADR 0051) is plugin-side code that lives with the packages: it is checked by
# `test_plugin_kit_stays_light` below instead, and core code may not import it either.
PLUGIN_SDK = "packages/py/ce_plugin_kit"


def _sources(*roots: str) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        out += [p for p in (ROOT / root).glob("*/src/**/*.py") if not str(p).startswith(str(ROOT / PLUGIN_SDK))]
    return out


def _module_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


# The worker runtime is the plugin host on GPU workers (ADR 0051): its smoke/bench harness uses the
# SDK's work units and GPU summaries, never a plugin package or an engine library.
SDK_HOSTS = {"ce_worker"}


def test_core_and_services_never_import_plugins_or_engine_libraries() -> None:
    offenders = []
    for path in _sources("packages/py", "apps"):
        package = path.relative_to(ROOT).parts[2]  # packages/py/<package>/… or apps/<app>/src/…
        allowed = {"ce_plugin_kit"} if package in SDK_HOSTS else set()
        bad = {
            n for n in _imports(path) if (n.startswith(PLUGIN_PREFIXES) or n in ENGINE_LIBRARIES) and n not in allowed
        }
        if bad:
            offenders.append((str(path.relative_to(ROOT)), sorted(bad)))
    assert offenders == []


def test_plugins_depend_on_contracts_not_on_core_services() -> None:
    offenders = []
    for path in ROOT.glob("plugins/**/src/**/*.py"):
        bad = _imports(path) & CORE_FORBIDDEN_IN_PLUGINS
        if bad:
            offenders.append((str(path.relative_to(ROOT)), sorted(bad)))
    assert offenders == []


def test_plugin_kit_stays_light() -> None:
    """The SDK every engine plugin imports: no core services at all, and engine libraries only
    inside functions (so plugins and their contract tests import it on CPU-only machines)."""
    sources = list((ROOT / PLUGIN_SDK).glob("src/**/*.py"))
    assert sources
    offenders = []
    for path in sources:
        bad = (_imports(path) & CORE_FORBIDDEN_IN_PLUGINS) | (_module_level_imports(path) & ENGINE_LIBRARIES)
        if bad:
            offenders.append((str(path.relative_to(ROOT)), sorted(bad)))
    assert offenders == []


def test_the_lint_sees_the_code_it_guards() -> None:
    assert len(_sources("packages/py", "apps")) > 100
    assert any(p.name == "graph.py" for p in _sources("packages/py"))
