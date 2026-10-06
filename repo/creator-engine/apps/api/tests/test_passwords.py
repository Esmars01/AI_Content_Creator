"""argon2id hashing with configured parameters."""

from __future__ import annotations

from ce_api.security.passwords import Passwords, password_issues
from ce_config.schemas import PasswordHashConfig

FAST = PasswordHashConfig(time_cost=1, memory_cost_kib=8192, parallelism=1)


def test_hash_and_verify() -> None:
    passwords = Passwords(FAST)
    digest = passwords.hash("correct horse battery")
    assert digest.startswith("$argon2id$v=19$m=8192,t=1,p=1$")
    assert passwords.verify(digest, "correct horse battery")
    assert not passwords.verify(digest, "correct horse batterY")
    assert not passwords.verify(None, "anything at all")
    assert not passwords.verify("not-a-hash", "anything at all")
    assert passwords.hash("same") != passwords.hash("same")  # salted


def test_parameter_changes_require_rehash() -> None:
    old = Passwords(FAST).hash("a long enough password")
    assert not Passwords(FAST).needs_rehash(old)
    assert Passwords(PasswordHashConfig()).needs_rehash(old)
    assert Passwords(PasswordHashConfig(time_cost=1, memory_cost_kib=8192, parallelism=1)).verify(
        old, "a long enough password"
    )


def test_password_rules() -> None:
    assert password_issues("twelve chars", 12) == []
    assert [i.code for i in password_issues("short", 12)] == ["password_too_short"]
    assert [i.code for i in password_issues(" padded password ", 12)] == ["password_characters"]
    assert [i.code for i in password_issues("tab\tinside password", 12)] == ["password_characters"]
    assert "password_too_long" in [i.code for i in password_issues("x" * 1025, 12)]
