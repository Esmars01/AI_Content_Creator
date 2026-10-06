"""All tables of §29. Importing this package registers every table on `Base.metadata`."""

from ce_db.models import assets, behavior, creators, memory, platform, research, tenancy, videos, worlds

__all__ = ["assets", "behavior", "creators", "memory", "platform", "research", "tenancy", "videos", "worlds"]
