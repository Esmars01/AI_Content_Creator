"""Phase 0: the repository layout of §8 exists (rule 2: structure before features)."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

from tests.phase0 import spec_index as spec

ROOT = spec.ROOT


def test_top_level_entries_exist() -> None:
    missing = [e for e in spec.top_level_entries() if not (ROOT / e.rstrip("/")).exists()]
    assert not missing, f"§8 top-level entries missing: {missing}"


def test_apps_exist() -> None:
    names = [a.rstrip("/") for a in spec.apps()]
    assert set(names) == {"web", "api", "orchestrator", "scheduler", "gpu-worker", "render-worker"}
    for name in names:
        manifest = "package.json" if name == "web" else "pyproject.toml"
        assert (ROOT / "apps" / name / manifest).is_file(), f"apps/{name}/{manifest} missing"


def test_python_packages_exist_with_py_typed() -> None:
    packages = spec.python_packages()
    assert len(packages) == 24, packages
    for pkg in packages:
        src = ROOT / "packages" / "py" / pkg / "src" / pkg
        assert (src / "__init__.py").is_file(), f"{pkg}: missing __init__.py"
        assert (src / "py.typed").is_file(), f"{pkg}: missing py.typed marker"


@pytest.mark.parametrize("pkg", spec.python_packages())
def test_python_package_imports_and_declares_phase(pkg: str) -> None:
    module = importlib.import_module(pkg)
    phase = module.IMPLEMENTED_IN_PHASE
    assert isinstance(phase, int) and 0 <= phase <= 14


APP_PACKAGES = {
    "api": "ce_api",
    "orchestrator": "ce_orchestrator",
    "scheduler": "ce_scheduler",
    "gpu-worker": "ce_gpu_worker",
    "render-worker": "ce_render_worker",
}


@pytest.mark.parametrize(("app", "module_name"), sorted(APP_PACKAGES.items()))
def test_app_packages_import_with_their_project_version(app: str, module_name: str) -> None:
    meta = tomllib.loads((ROOT / "apps" / app / "pyproject.toml").read_text(encoding="utf-8"))
    assert importlib.import_module(module_name).__version__ == meta["project"]["version"]


def test_uv_workspace_lists_every_python_member() -> None:
    root = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members = root["tool"]["uv"]["workspace"]["members"]
    found = {p.parent.relative_to(ROOT).as_posix() for p in ROOT.glob("packages/py/*/pyproject.toml")}
    found |= {p.parent.relative_to(ROOT).as_posix() for p in ROOT.glob("apps/*/pyproject.toml")}
    found |= {
        p.parent.relative_to(ROOT).as_posix() for p in ROOT.glob("plugins/**/pyproject.toml") if ".venv" not in p.parts
    }
    declared: set[str] = set()
    for pattern in members:
        declared |= {p.relative_to(ROOT).as_posix() for p in ROOT.glob(pattern) if (p / "pyproject.toml").exists()}
    assert found == declared


@pytest.mark.parametrize("pkg", ["ce_contracts", "ce_worker"])
def test_worker_shared_packages_support_python_310(pkg: str) -> None:
    """§7: packages GPU workers import must stay compatible with Python 3.10+."""
    import ast

    pkg_dir = ROOT / "packages" / "py" / pkg
    meta = tomllib.loads((pkg_dir / "pyproject.toml").read_text(encoding="utf-8"))
    assert meta["project"]["requires-python"] == ">=3.10"
    for source in (pkg_dir / "src").rglob("*.py"):
        ast.parse(source.read_text(encoding="utf-8"), filename=str(source), feature_version=(3, 10))


def test_plugin_categories_exist() -> None:
    categories = spec.plugin_categories()
    assert "mock" in categories and "providers" in categories
    missing = [c for c in categories if not (ROOT / "plugins" / c / "README.md").is_file()]
    assert not missing, f"plugin category READMEs missing: {missing}"


def test_config_dirs_and_env_files_exist() -> None:
    missing = [d for d in spec.config_dirs() if not (ROOT / "config" / d).is_dir()]
    assert not missing, f"config dirs missing: {missing}"
    for name in ("default.yaml", "env/dev.yaml", "env/test.yaml", "env/prod.yaml"):
        assert (ROOT / "config" / name).is_file(), name


def test_infra_and_tooling_files_exist() -> None:
    expected = [
        "infra/compose/docker-compose.yml",
        "infra/docker",
        "infra/observability",
        "infra/k8s",
        ".github/workflows/ci.yml",
        ".github/pull_request_template.md",
        ".pre-commit-config.yaml",
        "pnpm-workspace.yaml",
        "pnpm-lock.yaml",
        "uv.lock",
        "packages/ts/api-client/package.json",
        "apps/web/package.json",
        "tests/invariants",
    ]
    missing = [p for p in expected if not (ROOT / p).exists()]
    assert not missing, missing


def test_pr_template_carries_rule_20_checklist() -> None:
    template = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
    for item in (
        "Pydantic",
        "migration",
        "API",
        "UI",
        "plugin manifest",
        "build-graph",
        "router",
        "tests",
        "docs",
        "roadmap",
    ):
        assert item.lower() in template.lower(), f"PR template lacks rule-20 item: {item}"
    assert "invariant" in template.lower()


# The phase currently being built. Packages implemented in a later phase must still say they are
# skeletons; packages implemented up to this phase must describe their real status.
CURRENT_PHASE = 12


def test_no_stray_skeleton_claims() -> None:
    """Rule 5: never pretend something works. Later-phase packages say "skeleton only"."""
    for init in Path(ROOT / "packages" / "py").glob("*/src/*/__init__.py"):
        text = init.read_text(encoding="utf-8")
        module = importlib.import_module(init.parent.name)
        if module.IMPLEMENTED_IN_PHASE > CURRENT_PHASE:
            assert "skeleton only" in text.lower(), f"{init} does not mark itself as a skeleton"
        else:
            assert "status:" in text.lower(), f"{init} must state its implementation status"
