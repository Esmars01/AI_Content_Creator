"""Key rules and the provider registry (providers are plugins since Phase 2, ADR 0030)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from ce_core.errors import InvalidInputError
from ce_storage import asset_key, content_key, create_storage, provider_names, register_provider, validate_bucket
from ce_storage.base import PresignedEndpoint, StorageProvider, multipart_etag
from pydantic import SecretStr


@pytest.mark.parametrize("bucket", ["ce-assets", "abc", "a.b-c", "x" * 63])
def test_valid_buckets(bucket: str) -> None:
    assert validate_bucket(bucket) == bucket


@pytest.mark.parametrize("bucket", ["ab", "Upper", "x" * 64, "-ab", "ab-", "a..b", "a.-b", "192.168.1.1", "a_b"])
def test_invalid_buckets(bucket: str) -> None:
    with pytest.raises(InvalidInputError):
        validate_bucket(bucket)


def test_key_layout() -> None:
    org = UUID("01890000-0000-7000-8000-000000000001")
    asset = UUID("01890000-0000-7000-8000-000000000002")
    assert asset_key(org, asset) == f"orgs/{org}/assets/{asset}/original"
    digest = hashlib.sha256(b"x").hexdigest()
    assert content_key(digest) == f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
    with pytest.raises(InvalidInputError):
        content_key("ABC")


def test_multipart_etag_matches_the_s3_convention() -> None:
    a, b = hashlib.md5(b"a", usedforsecurity=False), hashlib.md5(b"b", usedforsecurity=False)
    expected = hashlib.md5(a.digest() + b.digest(), usedforsecurity=False).hexdigest() + "-2"
    assert multipart_etag([a.hexdigest(), f'"{b.hexdigest()}"']) == expected


def _settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "app_env": "test",
        "storage_provider": "s3",
        "s3_endpoint_url": "http://localhost:8333",
        "s3_public_endpoint_url": None,
        "s3_region": "us-east-1",
        "s3_access_key_id": SecretStr("k"),
        "s3_secret_access_key": SecretStr("s"),
        "local_storage_root": "/tmp/unused",
        "api_base_url": "http://localhost:8000",
        "secret_key": SecretStr("test-secret"),
    }
    return SimpleNamespace(**{**values, **overrides})


def test_registry_creates_the_configured_provider_from_plugins(tmp_path: Path) -> None:
    assert {"s3", "local_fs"} <= set(provider_names())
    s3 = create_storage(_settings())  # type: ignore[arg-type]
    assert s3.name == "s3" and isinstance(s3, StorageProvider)
    local = create_storage(_settings(storage_provider="local_fs", local_storage_root=str(tmp_path)))  # type: ignore[arg-type]
    assert local.name == "local_fs" and isinstance(local, PresignedEndpoint)
    with pytest.raises(ValueError, match="unknown STORAGE_PROVIDER"):
        create_storage(_settings(storage_provider="ftp"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="already registered"):
        register_provider("s3", lambda s: local)


def test_the_local_provider_is_not_offered_in_production() -> None:
    assert "local_fs" not in provider_names("prod")
    with pytest.raises(ValueError, match="unknown STORAGE_PROVIDER"):
        create_storage(_settings(app_env="prod", storage_provider="local_fs"))  # type: ignore[arg-type]
