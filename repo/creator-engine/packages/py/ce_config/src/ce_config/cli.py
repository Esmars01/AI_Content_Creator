"""`ce config …` commands (§35): validate the config tree, print digests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from ce_config.loader import ConfigError, load_config
from ce_config.settings import load_effective, startup_issues

app = typer.Typer(help="Configuration: validate the config tree and show digests.", no_args_is_help=True)

RootOption = Annotated[Path, typer.Option("--root", help="config directory")]
EnvOption = Annotated[str, typer.Option("--env", help="APP_ENV to resolve: dev | test | prod")]


@app.command()
def validate(root: RootOption = Path("config"), env: EnvOption = "dev", startup: bool = False) -> None:
    """Validate every YAML (schemas, vocabularies, cross-references). Exit 1 on errors."""
    try:
        bundle = load_config(root, env)
    except ConfigError as exc:
        typer.echo(f"ERROR {exc}", err=True)
        raise typer.Exit(1) from exc
    issues = list(bundle.issues)
    if startup:
        issues = startup_issues(load_effective(root, {**os.environ, "APP_ENV": env}))
    for issue in issues:
        typer.echo(f"{issue.severity.upper():7} {issue.code:20} {issue.path or '-':40} {issue.message}")
    errors = [i for i in issues if i.severity == "error"]
    typer.echo(
        f"{len(bundle.digests)} files, vocab_version {bundle.vocab.version}: "
        f"{len(errors)} errors, {len(issues) - len(errors)} warnings"
    )
    if errors:
        raise typer.Exit(1)


@app.command()
def digests(root: RootOption = Path("config"), env: EnvOption = "dev") -> None:
    """Print the config_digest of every file (what cache keys include, §12.2)."""
    bundle = load_config(root, env)
    typer.echo(json.dumps(bundle.digests, indent=2, sort_keys=True))
