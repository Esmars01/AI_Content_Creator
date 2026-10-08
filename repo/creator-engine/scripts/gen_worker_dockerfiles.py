#!/usr/bin/env python3
"""Renders the GPU runtime-family Dockerfiles (`infra/docker/worker-<family>.Dockerfile`, §36, ADR 0052)
from `infra/docker/families.yaml` and the plugin manifests.

Each family Dockerfile has one build target per variant (`docker build --target <variant>`). A variant
image holds: the CUDA base pinned by digest, a uv-managed Python of the manifests' version, the variant's
torch wheels, its upstream repositories cloned at the manifests' pinned revisions, every `pypi:` pin its
manifests declare, the 3.10-compatible worker packages (`ce_contracts`, `ce_plugin_kit`, `ce_worker`) and
the variant's plugin packages. Weights are never baked in: workers fetch them into the model cache.

`--check` fails when a committed Dockerfile differs from what the generator renders, when a GPU adapter is
not in exactly one variant, when a variant mixes families or Python versions, or when a manifest's `git`
dependency has no repository entry (or the reverse).

Usage: uv run python scripts/gen_worker_dockerfiles.py [--check]
"""

from __future__ import annotations

import argparse
import importlib
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
FAMILIES = ROOT / "infra" / "docker" / "families.yaml"
OUT_DIR = ROOT / "infra" / "docker"
VARIANTS_OUT = ROOT / "config" / "gpu" / "variants.yaml"
GPU_FAMILIES = ("image", "wan", "tts", "asr", "audio", "vllm", "post", "lipsync", "vision")  # §36
WORKER_PACKAGES = ("packages/py/ce_contracts", "packages/py/ce_plugin_kit", "packages/py/ce_worker")
GITHUB_REF = re.compile(r"^github:(?P<repo>[\w.-]+/[\w.-]+)@(?P<rev>[0-9a-f]{7,40})")
PYPI_REF = re.compile(r"^pypi:(?P<spec>[A-Za-z0-9_.\[\]-]+==[\w.+-]+)")


def manifests() -> dict[str, Any]:
    from ce_contracts.plugins import discover

    registry = discover(app_env=None, include_mocks=False)
    return {pid: p for pid, p in registry.plugins.items() if p.manifest.runtime.family in GPU_FAMILIES}


def plugin_dir(plugin: Any) -> str:
    module = importlib.import_module(plugin.manifest.entrypoint.split(":")[0].split(".")[0])
    path = Path(module.__file__ or "").resolve()
    for parent in path.parents:
        if (parent / "pyproject.toml").is_file() and parent != ROOT:
            return str(parent.relative_to(ROOT))
    raise SystemExit(f"{plugin.id}: no pyproject.toml above {path}")


def code_and_pins(manifest: Any) -> tuple[dict[str, str], list[str]]:
    """Git repositories (URL → revision) and PyPI pins the manifest's models depend on."""
    repos: dict[str, str] = {}
    pins: list[str] = []
    for decl in manifest.models:
        if decl.source.type == "git" and decl.source.repo:
            repos[decl.source.repo.rstrip("/")] = decl.source.revision
        for dep in decl.dependencies:
            if dep.source is not None and dep.source.type == "git" and dep.source.repo:
                repos[dep.source.repo.rstrip("/")] = dep.source.revision
                continue
            github = GITHUB_REF.match(dep.ref or "")
            if github:
                repos[f"https://github.com/{github['repo']}"] = github["rev"]
            pypi = PYPI_REF.match(dep.ref or "")
            if pypi:
                pins.append(pypi["spec"])
    return repos, sorted(set(pins))


def plan(config: dict[str, Any], plugins: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    errors: list[str] = []
    seen: dict[str, str] = {}
    families: dict[str, list[dict[str, Any]]] = {}
    for name, variant in config["variants"].items():
        adapters = list(variant["adapters"])
        for adapter in adapters:
            if adapter not in plugins:
                errors.append(f"{name}: {adapter} is not an installed GPU adapter")
                continue
            if adapter in seen:
                errors.append(f"{adapter} is in variants {seen[adapter]} and {name}")
            seen[adapter] = name
        known = [plugins[a] for a in adapters if a in plugins]
        fams = {p.manifest.runtime.family for p in known}
        pythons = {p.manifest.runtime.python for p in known}
        if fams != {variant["family"]}:
            errors.append(f"{name}: declared family {variant['family']}, adapters are {sorted(fams)}")
        if len(pythons) > 1:
            errors.append(f"{name}: adapters need different Pythons {sorted(pythons)}")
        repos: dict[str, str] = {}
        pins: set[str] = set()
        for plugin in known:
            r, p = code_and_pins(plugin.manifest)
            for url, rev in r.items():
                if repos.get(url, rev) != rev:
                    errors.append(f"{name}: {url} pinned at {repos[url]} and {rev}")
                repos[url] = rev
            pins |= set(p)
        declared = {r["url"].rstrip("/"): r for r in variant.get("repos", [])}
        for url in sorted(set(repos) - set(declared)):
            errors.append(f"{name}: manifest code {url} has no repos entry (how is it installed?)")
        for url in sorted(set(declared) - set(repos)):
            errors.append(f"{name}: repos entry {url} is not a dependency of its adapters' manifests")
        families.setdefault(variant["family"], []).append(
            {
                "name": name,
                "adapters": adapters,
                "python": next(iter(pythons)) if pythons else "3.10",
                "base": variant.get("base", "runtime"),
                "torch": variant.get("torch"),
                "pip": list(variant.get("pip", [])),
                "apt": list(variant.get("apt", [])),
                "pins": sorted(pins),
                "repos": [{**declared[u], "revision": repos[u]} for u in sorted(declared) if u in repos],
                "plugins": sorted({plugin_dir(plugins[a]) for a in adapters if a in plugins}),
            }
        )
    for adapter in sorted(set(plugins) - set(seen)):
        errors.append(f"{adapter} ({plugins[adapter].manifest.runtime.family}) is in no variant")
    return families, errors


def _repo_dir(url: str) -> str:
    return "/opt/upstream/" + url.rstrip("/").rsplit("/", 1)[-1]


def _stage(variant: dict[str, Any], config: dict[str, Any], *, stage: str, venv: str) -> tuple[list[str], list[str]]:
    """The build steps of one variant (its environment at `venv`) and the PYTHONPATH it needs."""
    name = variant["name"]
    base = config["bases"][variant["base"]]
    apt = " ".join(["ca-certificates", "ffmpeg", "git", "curl", *variant["apt"]])
    lines = [
        f"# ---------------------------------------------------------------- {stage}",
        f"FROM {base} AS {stage}",
        "ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \\",
        f"    UV_PYTHON_INSTALL_DIR=/opt/python VIRTUAL_ENV={venv} PATH={venv}/bin:$PATH",
        f"RUN apt-get update && apt-get install -y --no-install-recommends {apt} \\",
        "    && rm -rf /var/lib/apt/lists/* && useradd --create-home --uid 10001 ce && mkdir -p /models /app \\",
        "    && chown ce:ce /models",
        "COPY --from=uv /uv /usr/local/bin/uv",
        "RUN --mount=type=secret,id=extra_ca,required=false \\",
        "    if [ -s /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \\",
        f"    uv python install {variant['python']} && uv venv {venv} --python {variant['python']}",
    ]
    torch = variant["torch"]
    if torch:
        index = f"https://download.pytorch.org/whl/{torch['index']}"
        lines += [
            "RUN --mount=type=cache,target=/root/.cache/uv --mount=type=secret,id=extra_ca,required=false \\",
            "    if [ -s /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \\",
            f"    uv pip install --index-url {index} {' '.join(torch['packages'])}",
        ]
    pythonpath: list[str] = []
    for repo in variant["repos"]:
        if repo["install"] == "reference":  # license evidence of code the pipeline does not import
            lines.append(f"# {repo['url']}@{repo['revision']}: license reference only (not installed)")
            continue
        target = _repo_dir(repo["url"])
        lines += [
            f"RUN git init -q {target} && git -C {target} remote add origin {repo['url']} \\",
            f"    && git -C {target} fetch -q --depth 1 origin {repo['revision']} \\",
            f"    && git -C {target} checkout -q FETCH_HEAD",
        ]
        install = repo["install"]
        extra = f" --extra-index-url https://download.pytorch.org/whl/{torch['index']}" if torch else ""
        prefix = (
            "RUN --mount=type=cache,target=/root/.cache/uv --mount=type=secret,id=extra_ca,required=false \\\n"
            "    if [ -s /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \\\n    "
        )
        if install == "requirements":
            files = repo.get("requirements", ["requirements.txt"])
            reqs = " ".join(f"-r {target}/{f}" for f in files)
            lines.append(prefix + f"uv pip install{extra} {reqs}")
            pythonpath.append(target)
        elif install == "package":
            lines.append(prefix + f"uv pip install{extra} {target}")
        elif install == "apex":  # NVIDIA apex: the CUDA extensions the configs use (needs the devel base)
            lines.append(
                prefix + f'uv pip install --no-build-isolation --config-settings "--build-option=--cpp_ext" '
                f'--config-settings "--build-option=--cuda_ext" {target}'
            )
        elif install == "path":
            pythonpath.append(target)
        else:
            raise SystemExit(f"{name}: unknown install method {install!r}")
    packages = [*variant["pins"], *variant["pip"]]
    if packages:
        extra = f" --extra-index-url https://download.pytorch.org/whl/{torch['index']}" if torch else ""
        quoted = " ".join(f'"{p}"' if "[" in p else p for p in packages)
        if any(p.startswith("flash_attn") for p in packages):  # compiles against the installed torch
            extra += " --no-build-isolation-package flash_attn"
        lines += [
            "RUN --mount=type=cache,target=/root/.cache/uv --mount=type=secret,id=extra_ca,required=false \\",
            "    if [ -s /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \\",
            f"    uv pip install{extra} {quoted}",
        ]
    sources = [*WORKER_PACKAGES, *variant["plugins"]]
    for src in sources:
        lines.append(f"COPY {src} /src/{src}")
    lines += [
        "RUN --mount=type=cache,target=/root/.cache/uv --mount=type=secret,id=extra_ca,required=false \\",
        "    if [ -s /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \\",
        "    uv pip install " + " ".join(f"/src/{s}" for s in sources),
    ]
    return lines, pythonpath


def render(family: str, variants: list[dict[str, Any]], config: dict[str, Any]) -> str:
    lines = [
        "# syntax=docker/dockerfile:1.7",
        "# GENERATED by scripts/gen_worker_dockerfiles.py from infra/docker/families.yaml and the plugin manifests —",
        "# do not edit by hand. The `" + family + "` GPU runtime family (§36, ADR 0052): one build target per variant.",
        "#   docker build -f infra/docker/worker-"
        + family
        + ".Dockerfile --target <variant> -t creator-engine/worker-"
        + family
        + ":<variant> .",
        "# Variants: " + ", ".join(f"{v['name']} ({', '.join(v['adapters'])})" for v in variants) + ".",
        "# Status: lint-checked only; never built with its CUDA stack here (no GPU, rule 5). Weights come from the",
        "# model cache at run time (MODEL_CACHE_DIR, a network volume or local disk), never from the image.",
        "",
        f"FROM {config['uv']} AS uv",
        "",
    ]
    for variant in variants:
        stage, pythonpath = _stage(variant, config, stage=variant["name"], venv="/app/.venv")
        lines += stage
        env = [
            f"WORKER_RUNTIME_FAMILY={family}",
            f"WORKER_ADAPTERS={','.join(variant['adapters'])}",
            "APP_ENV=prod",
            "MODEL_CACHE_DIR=/models",
        ]
        if pythonpath:
            env.append("PYTHONPATH=" + ":".join(pythonpath))
        lines += [
            "ENV " + " ".join(env),
            "WORKDIR /app",
            "USER ce",
            'CMD ["python", "-m", "ce_worker"]',
            "",
        ]
    return "\n".join(lines)


def plan_profiles(
    config: dict[str, Any], families: dict[str, list[dict[str, Any]]]
) -> tuple[dict[str, Any], list[str]]:
    """`profiles:` — one image serving several variants of different families side by side (cutover §14):
    each variant keeps its own environment (they pin different torch stacks), `ce_worker.multi` runs one
    worker process per family."""
    by_name = {v["name"]: (fam, v) for fam, variants in families.items() for v in variants}
    out: dict[str, Any] = {}
    errors: list[str] = []
    for name, profile in (config.get("profiles") or {}).items():
        components = []
        for variant_name in profile.get("components", []):
            if variant_name not in by_name:
                errors.append(f"profile {name}: {variant_name} is not a variant")
                continue
            components.append(by_name[variant_name])
        families_used = [fam for fam, _ in components]
        if len(set(families_used)) != len(families_used):
            errors.append(f"profile {name}: one variant per family (ce_worker.multi runs one worker per family)")
        if len({v["python"] for _, v in components}) > 1:
            errors.append(f"profile {name}: its variants need one Python (they share /opt/python)")
        out[name] = components
    return out, errors


def render_profile(name: str, components: list[tuple[str, dict[str, Any]]], config: dict[str, Any]) -> str:
    """`worker-profile-<name>.Dockerfile`: every component built in its own stage and environment
    (`/opt/env/<variant>`), then copied side by side into one image whose entrypoint runs one worker per family."""
    lines = [
        "# syntax=docker/dockerfile:1.7",
        "# GENERATED by scripts/gen_worker_dockerfiles.py from infra/docker/families.yaml and the plugin manifests —",
        f"# do not edit by hand. The `{name}` worker profile (cutover §14): one GPU instance, one worker per family.",
        f"#   docker build -f infra/docker/worker-profile-{name}.Dockerfile -t creator-engine/worker-profile:{name} .",
        "# Components: " + ", ".join(f"{v['name']} ({fam}: {', '.join(v['adapters'])})" for fam, v in components) + ".",
        "# Each keeps its own environment (they pin different torch stacks); `python -m ce_worker.multi` starts one",
        "# `ce_worker` per family with that family's WORKER_TOKEN_<FAMILY> and exits when one of them stops.",
        "# Status: lint-checked only; never built with its CUDA stack here (no GPU, rule 5). Weights come from the",
        "# model cache at run time (MODEL_CACHE_DIR), never from the image.",
        "",
        f"FROM {config['uv']} AS uv",
        "",
    ]
    pythonpaths: dict[str, list[str]] = {}
    for fam, variant in components:
        stage, pythonpath = _stage(variant, config, stage=f"{variant['name']}_env", venv=f"/opt/env/{variant['name']}")
        lines += [*stage, ""]
        pythonpaths[fam] = pythonpath
    devel = any(v["base"] == "devel" for _, v in components)
    base = config["bases"]["devel" if devel else components[0][1]["base"]]
    apt = sorted({"ca-certificates", "ffmpeg", "git", "curl", *(a for _, v in components for a in v["apt"])})
    lines += [
        f"# ---------------------------------------------------------------- {name}",
        f"FROM {base} AS {name}",
        "ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1",
        f"RUN apt-get update && apt-get install -y --no-install-recommends {' '.join(apt)} \\",
        "    && rm -rf /var/lib/apt/lists/* && useradd --create-home --uid 10001 ce && mkdir -p /models /app \\",
        "    && chown ce:ce /models",
    ]
    for _, variant in components:
        # every stage's managed Python (each environment links to the one it was created with)
        lines.append(f"COPY --from={variant['name']}_env /opt/python /opt/python")
        lines.append(f"COPY --from={variant['name']}_env /opt/env/{variant['name']} /opt/env/{variant['name']}")
        for repo in variant["repos"]:
            if repo["install"] in ("requirements", "path", "package"):
                target = _repo_dir(repo["url"])
                lines.append(f"COPY --from={variant['name']}_env {target} {target}")
    env = [f"WORKER_COMPONENTS={','.join(fam for fam, _ in components)}", f"WORKER_PROFILE={name}"]
    for fam, variant in components:
        key = fam.upper()
        env += [
            f"WORKER_PYTHON_{key}=/opt/env/{variant['name']}/bin/python",
            f"WORKER_ADAPTERS_{key}={','.join(variant['adapters'])}",
        ]
        if pythonpaths[fam]:
            env.append(f"WORKER_PYTHONPATH_{key}=" + ":".join(pythonpaths[fam]))
    env += ["APP_ENV=prod", "MODEL_CACHE_DIR=/models"]
    first = components[0][1]["name"]
    lines += [
        "ENV " + " \\\n    ".join(env),
        "WORKDIR /app",
        "USER ce",
        f'CMD ["/opt/env/{first}/bin/python", "-m", "ce_worker.multi"]',
        "",
    ]
    return "\n".join(lines)


def render_variants(families: dict[str, list[dict[str, Any]]]) -> str:
    """`config/gpu/variants.yaml`: adapter → the family image variant that serves it (fleet manager)."""
    lines = [
        "# GENERATED by scripts/gen_worker_dockerfiles.py from infra/docker/families.yaml — do not edit.",
        "# Which family image variant serves each GPU adapter (ADR 0052); the fleet manager provisions the",
        "# variant that serves the adapters with the largest backlog (Phase 9).",
        "variants:",
    ]
    rows = sorted((a, fam, v["name"]) for fam, variants in families.items() for v in variants for a in v["adapters"])
    lines += [f"  {adapter}: {{ family: {fam}, variant: {name} }}" for adapter, fam, name in rows]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail when a committed Dockerfile is stale")
    args = parser.parse_args()
    config = yaml.safe_load(FAMILIES.read_text(encoding="utf-8"))
    families, errors = plan(config, manifests())
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    stale = []
    for family in GPU_FAMILIES:
        variants = families.get(family)
        path = OUT_DIR / f"worker-{family}.Dockerfile"
        if not variants:
            print(f"note: no adapter of family {family} is installed", file=sys.stderr)
            continue
        text = render(family, variants, config)
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.write_text(text, encoding="utf-8")
    profiles, profile_errors = plan_profiles(config, families)
    if profile_errors:
        print("\n".join(profile_errors), file=sys.stderr)
        return 1
    for name, components in profiles.items():
        path = OUT_DIR / f"worker-profile-{name}.Dockerfile"
        text = render_profile(name, components, config)
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.write_text(text, encoding="utf-8")
    variants_text = render_variants(families)
    if args.check:
        if not VARIANTS_OUT.is_file() or VARIANTS_OUT.read_text(encoding="utf-8") != variants_text:
            stale.append(str(VARIANTS_OUT.relative_to(ROOT)))
    else:
        VARIANTS_OUT.write_text(variants_text, encoding="utf-8")
    if stale:
        print("stale (run scripts/gen_worker_dockerfiles.py): " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
