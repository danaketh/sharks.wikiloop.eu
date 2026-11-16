from uuid import UUID

from strata_cms.application.content.picker import (
    PICKER_QUERY_MAX_LENGTH,
    ContentTitleRow,
    search_picker_titles,
)
from strata_cms.domain.value_objects import ContentId, ContentTypeKey

ARTICLE = ContentTypeKey("tests.article")


class _RecordingQueries:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def search_titles(
        self,
        *,
        type_keys: frozenset[ContentTypeKey],
        query: str,
        limit: int,
        offset: int,
    ) -> tuple[tuple[ContentTitleRow, ...], int]:
        self.calls.append(
            {"type_keys": type_keys, "query": query, "limit": limit, "offset": offset}
        )
        return (), 0


def test_search_strips_and_caps_the_query() -> None:
    queries = _RecordingQueries()

    search_picker_titles(
        queries,
        type_keys=frozenset({ARTICLE}),
        query="  " + "x" * (PICKER_QUERY_MAX_LENGTH + 50) + "  ",
        limit=10,
        offset=0,
    )

    assert queries.calls[0]["query"] == "x" * PICKER_QUERY_MAX_LENGTH


def test_search_passes_blank_query_through_as_empty() -> None:
    queries = _RecordingQueries()

    rows, total = search_picker_titles(
        queries, type_keys=frozenset({ARTICLE}), query="   ", limit=5, offset=2
    )

    assert (rows, total) == ((), 0)
    assert queries.calls == [
        {"type_keys": frozenset({ARTICLE}), "query": "", "limit": 5, "offset": 2}
    ]


def test_title_row_is_a_value_object() -> None:
    row = ContentTitleRow(
        content_id=ContentId(UUID(int=1)),
        type_key=ARTICLE,
        title="A",
        is_published=False,
    )

    assert row.title == "A"
