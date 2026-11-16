"""Application-owned port for installed content-type behavior."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from strata_cms.application.errors import ContentDataProblem
from strata_cms.domain.revision import Revision, RevisionData
from strata_cms.domain.value_objects import ContentTypeKey


@dataclass(frozen=True, slots=True)
class PreparedContentData:
    """Validated current-version snapshot ready for immutable persistence."""

    schema_version: int
    data: RevisionData


@dataclass(frozen=True, slots=True)
class ContentLink:
    """One declared link found in revision data.

    ``location`` is a ``/``-joined key/index trail (``~`` and ``/`` escaped as
    ``~0``/``~1``) that identifies blocks by UUID; it is the stable key stored
    in link projections. ``path`` is the ``$.``-style JSON path of the same
    value (block list indexes, e.g. ``$.body[0].data.page``) used for
    validation problems. ``allowed_types`` is empty for blob links.
    """

    kind: Literal["reference", "blob"]
    location: str
    target: UUID
    allowed_types: frozenset[ContentTypeKey]
    path: str


@dataclass(frozen=True, slots=True)
class ContentLinks:
    """All declared content references and blob links of one revision."""

    references: tuple[ContentLink, ...] = ()
    blobs: tuple[ContentLink, ...] = ()


class ContentTypeService(Protocol):
    """Validate and decode content without exposing registry implementation."""

    def prepare_for_write(
        self,
        *,
        type_key: ContentTypeKey,
        schema_version: int,
        data: Mapping[str, object],
    ) -> PreparedContentData:
        """Upgrade/validate input and return canonical current-version data."""
        ...  # pragma: no cover - protocol declaration

    def validate_revision(self, revision: Revision) -> None:
        """Require that one revision remains decodable by an installed type."""
        ...  # pragma: no cover - protocol declaration

    def publish_issues(self, revision: Revision) -> tuple[ContentDataProblem, ...]:
        """Return publish-only readiness problems for one revision."""
        ...  # pragma: no cover - protocol declaration

    def delivery_data(self, revision: Revision) -> dict[str, object]:
        """Return current public JSON for one historical revision."""
        ...  # pragma: no cover - protocol declaration

    def extract_links(
        self,
        *,
        type_key: ContentTypeKey,
        data: RevisionData,
    ) -> ContentLinks:
        """Return declared links of canonical current-version data."""
        ...  # pragma: no cover - protocol declaration

    def summary_title(self, revision: Revision) -> str | None:
        """Return the human summary title, or None when the type declares none."""
        ...  # pragma: no cover - protocol declaration

    def embeds_in_references(self, type_key: ContentTypeKey) -> bool:
        """Return whether reference summaries embed this type's delivery data."""
        ...  # pragma: no cover - protocol declaration

    def blob_ids(self, revision: Revision) -> frozenset[UUID]:
        """Return every blob id linked from one (possibly historical) revision."""
        ...  # pragma: no cover - protocol declaration
