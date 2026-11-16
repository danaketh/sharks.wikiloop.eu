"""Media blob entity: immutable stored bytes with a stable UUID identity."""

import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from strata_cms.domain.value_objects import ActorId

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class MediaBlob:
    """One stored, validated media payload referenced from revisions.

    ``storage_key`` locates the bytes but is not the identity; ``id`` is.
    """

    id: UUID
    storage_key: str
    sha256: str
    content_type: str
    size_bytes: int
    width: int
    height: int
    created_at: datetime
    created_by: ActorId

    def __post_init__(self) -> None:
        """Enforce basic integrity of the stored-bytes description."""
        if not self.storage_key:
            raise ValueError("Media blob storage key must not be empty.")
        if not _SHA256_HEX.fullmatch(self.sha256):
            raise ValueError("Media blob sha256 must be 64 lowercase hex chars.")
        if self.size_bytes < 0:
            raise ValueError("Media blob size must not be negative.")
        if self.width < 1 or self.height < 1:
            raise ValueError("Media blob dimensions must be positive.")
        if self.created_at.tzinfo is None:
            raise ValueError("Media blob created_at must be timezone-aware.")
