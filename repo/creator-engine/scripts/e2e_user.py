"""A throwaway editor in the dev organization for end-to-end tests against the running stack.

`uv run python scripts/e2e_user.py` prints `{"email": …, "password": …, "org_id": …}`. The script
runs next to the Compose stack and writes to its database (`DATABASE_URL` from `.env`); it is
test plumbing — the product has no self-registration (invitations, §30).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


async def create_editor(role: str = "editor") -> dict[str, str]:
    from ce_api.security.passwords import Passwords
    from ce_config.settings import load_effective
    from ce_db.models.tenancy import Membership, User
    from ce_db.session import Database
    from ce_testing.fixtures import ALEX

    security = load_effective(ROOT / "config").bundle.app.security
    password = secrets.token_urlsafe(18)
    email = f"e2e-{secrets.token_hex(4)}@example.test"
    db = Database(os.environ["DATABASE_URL"], pool_size=1)
    try:
        async with db.transaction() as session:
            user = User(email=email, name="e2e", password_hash=Passwords(security.password_hash).hash(password))
            session.add(user)
            await session.flush()
            session.add(Membership(user_id=user.id, org_id=ALEX.ORG_ID, role=role))
    finally:
        await db.dispose()
    return {"email": email, "password": password, "org_id": str(ALEX.ORG_ID)}


if __name__ == "__main__":
    load_env()
    print(json.dumps(asyncio.run(create_editor(sys.argv[1] if len(sys.argv) > 1 else "editor"))))
