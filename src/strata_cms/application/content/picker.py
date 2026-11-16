"""Title search used by management reference pickers."""

from dataclasses import dataclass
from typing import Protocol

from strata_cms.domain.value_objects import ContentId, ContentTypeKey

PICKER_QUERY_MAX_LENGTH = 200
"""Longest search text honoured; longer input is truncated, not rejected."""


@dataclass(frozen=True, slots=True)
class ContentTitleRow:
    """One searchable, non-archived content item in the latest-draft scope."""

    content_id: ContentId
    type_key: ContentTypeKey
    title: str
    is_published: bool


class ContentTitleSearch(Protocol):
    """The title-search capability of the editor query port."""

    def search_titles(
        self,
        *,
        type_keys: frozenset[ContentTypeKey],
        query: str,
        limit: int,
        offset: int,
    ) -> tuple[tuple[ContentTitleRow, ...], int]:
        """Return one page of matching rows and the total match count."""
        ...  # pragma: no cover - protocol declaration


def search_picker_titles(
    queries: ContentTitleSearch,
    *,
    type_keys: frozenset[ContentTypeKey],
    query: str,
    limit: int,
    offset: int,
) -> tuple[tuple[ContentTitleRow, ...], int]:
    """Search draft titles of the given types; an empty query lists them all."""
    normalized = query.strip()[:PICKER_QUERY_MAX_LENGTH]
    return queries.search_titles(
        type_keys=type_keys, query=normalized, limit=limit, offset=offset
    )
