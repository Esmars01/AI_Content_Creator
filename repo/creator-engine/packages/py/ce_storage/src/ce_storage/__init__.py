"""StorageProvider interface, registry and the shared contract suite.

Status: implemented and tested in Phase 1; since Phase 2 the providers are plugins
(`plugins/providers/storage/{s3,local_fs}`, ADR 0030) discovered through entry points. Both pass
the contract suite (`ce_storage.contract`); the S3 provider is tested against SeaweedFS.
"""

from ce_storage.base import (
    LOCAL_ROUTE_PREFIX,
    MIN_PART_BYTES,
    CompletedPart,
    ListedObject,
    MultipartUpload,
    ObjectInfo,
    PresignedEndpoint,
    PresignedRequest,
    PresignedResponse,
    StorageProvider,
)
from ce_storage.keys import asset_key, content_key, validate_bucket, validate_key
from ce_storage.registry import create_storage, provider_names, register_provider

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 1

__all__ = [
    "LOCAL_ROUTE_PREFIX",
    "MIN_PART_BYTES",
    "CompletedPart",
    "ListedObject",
    "MultipartUpload",
    "ObjectInfo",
    "PresignedEndpoint",
    "PresignedRequest",
    "PresignedResponse",
    "StorageProvider",
    "asset_key",
    "content_key",
    "create_storage",
    "provider_names",
    "register_provider",
    "validate_bucket",
    "validate_key",
]
