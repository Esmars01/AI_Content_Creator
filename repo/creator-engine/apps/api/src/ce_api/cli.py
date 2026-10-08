"""The `ce` admin CLI (§7: Typer). Sub-commands come from the packages that own them."""

from __future__ import annotations

import typer
from ce_config.cli import app as config_app

from ce_api.admin_cli import admin_app, data_app
from ce_api.seed_cli import seed_app, storage_app
from ce_api.video_cli import video_app

app = typer.Typer(help="Creator Engine admin CLI.", no_args_is_help=True)
app.add_typer(admin_app, name="admin")
app.add_typer(config_app, name="config")
app.add_typer(data_app, name="data")
app.add_typer(seed_app, name="seed")
app.add_typer(storage_app, name="storage")
app.add_typer(video_app, name="video")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
