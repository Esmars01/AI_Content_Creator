"""Phase 0 Definition of Done: the core infrastructure is up, healthy and usable.

Each test exercises the capability later phases depend on, not just an open port:
Postgres 18 with pgvector, Redis Streams on Valkey (SSE event log), a Temporal workflow
round trip with the Pydantic data converter (§7), and S3 object I/O plus presigned URLs on
SeaweedFS (§10 ADR 0010).
"""

from __future__ import annotations

import os
import urllib.request
import uuid
from datetime import timedelta
from typing import cast

import asyncpg
import boto3
import pytest
import redis
from botocore.config import Config
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from tests.infra.smoke_workflows import Greeting, SmokeWorkflow, compose_greeting

pytestmark = pytest.mark.infra


def _pg_dsn() -> str:
    # DATABASE_URL uses the SQLAlchemy driver prefix; asyncpg wants a plain postgresql:// DSN.
    return os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)


async def test_postgres_is_18_with_pgvector() -> None:
    conn = await asyncpg.connect(_pg_dsn())
    try:
        version = await conn.fetchval("SHOW server_version_num")
        assert int(version) >= 180000, f"expected PostgreSQL 18, got {version}"
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        distance = await conn.fetchval("SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector")
        assert distance == pytest.approx(1.0)
        ext_version = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        assert ext_version
    finally:
        await conn.close()


def test_redis_streams_roundtrip() -> None:
    client = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    assert client.ping() is True
    stream = f"ce:test:{uuid.uuid4().hex}"
    try:
        first = str(client.xadd(stream, {"type": "job.updated", "n": "1"}))
        client.xadd(stream, {"type": "job.updated", "n": "2"})
        # Resume after the first event, as SSE Last-Event-ID replay will (§30).
        replay = cast(list[tuple[str, dict[str, str]]], client.xrange(stream, min=f"({first}"))
        assert [fields["n"] for _, fields in replay] == ["2"]
    finally:
        client.delete(stream)



async def test_temporal_workflow_roundtrip_with_pydantic_converter() -> None:
    client = await Client.connect(
        os.environ["TEMPORAL_ADDRESS"],
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        data_converter=pydantic_data_converter,
    )
    task_queue = f"phase0-smoke-{uuid.uuid4().hex[:8]}"
    async with Worker(client, task_queue=task_queue, workflows=[SmokeWorkflow], activities=[compose_greeting]):
        result = await client.execute_workflow(
            SmokeWorkflow.run,
            Greeting(name="Alex", excited=True),
            id=f"phase0-smoke-{uuid.uuid4().hex}",
            task_queue=task_queue,
            execution_timeout=timedelta(seconds=30),
        )
    assert result == "Hello, Alex!"


def test_seaweedfs_s3_object_io_and_presigned_url() -> None:
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("S3_REGION", "us-east-1"),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    bucket = f"ce-phase0-{uuid.uuid4().hex[:8]}"
    key = "probe/hello.txt"
    s3.create_bucket(Bucket=bucket)
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=b"creator-engine")
        assert s3.get_object(Bucket=bucket, Key=key)["Body"].read() == b"creator-engine"
        url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=60)
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - local presigned URL
            assert response.read() == b"creator-engine"
    finally:
        s3.delete_object(Bucket=bucket, Key=key)
        s3.delete_bucket(Bucket=bucket)


def test_temporal_ui_serves_http() -> None:
    port = os.environ.get("TEMPORAL_UI_PORT", "8080")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as response:  # noqa: S310 - local URL
        assert response.status == 200
