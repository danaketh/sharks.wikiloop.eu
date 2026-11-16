"""Read-only application service backing the public Delivery API."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from strata_cms.application.content.links import LinkResolver
from strata_cms.application.ports.cache import Cache
from strata_cms.application.ports.content_delivery import (
    ContentDeliveryQueries,
    PublishedRevision,
)
from strata_cms.application.ports.content_types import ContentTypeService
from strata_cms.application.ports.links import (
    PublishedLinkQueries,
    PublishedReferenceIndex,
)
from strata_cms.domain.value_objects import ContentId, ContentTypeKey, RevisionId

DEFAULT_DELIVERY_CACHE_TTL = timedelta(seconds=60)
"""Short TTL, disposable cache-aside: no active invalidation on publish/archive."""


@dataclass(frozen=True, slots=True)
class PublishedContent:
    """Delivery-ready published content for the public Delivery API."""

    content_id: ContentId
    type_key: ContentTypeKey
    revision_id: RevisionId
    revision_number: int
    published_at: datetime
    data: dict[str, object]


@dataclass(frozen=True, slots=True)
class PublishedContentPage:
    """One page of published content for the public Delivery API."""

    items: tuple[PublishedContent, ...]
    total_count: int
    limit: int
    offset: int


def get_published_content(
    content_id: ContentId,
    *,
    queries: ContentDeliveryQueries,
    content_types: ContentTypeService,
    cache: Cache | None = None,
    cache_ttl: timedelta = DEFAULT_DELIVERY_CACHE_TTL,
    links: LinkResolver | None = None,
) -> PublishedContent | None:
    """Return the current public revision rendered for delivery, if published.

    With ``links``, ``$ref``/``$blob`` links are resolved before caching, so the
    cached value is the resolved output (refreshed only by the cache TTL).
    """
    cache_key = f"strata:delivery:content:{content_id}"
    if cache is not None:
        cached = cache.get(cache_key)
        if isinstance(cached, PublishedContent):
            return cached

    published = queries.get_published(content_id)
    if published is None:
        return None
    data = content_types.delivery_data(published.revision)
    if links is not None:
        data = links.resolve(data)
    result = PublishedContent(
        content_id=published.content_id,
        type_key=published.type_key,
        revision_id=published.revision.id,
        revision_number=published.revision.number,
        published_at=published.published_at,
        data=data,
    )
    if cache is not None:
        cache.set(cache_key, result, ttl=cache_ttl)
    return result


def list_published_content(  # noqa: PLR0913 - keyword-only injected ports, one per dependency
    *,
    type_key: ContentTypeKey | None,
    limit: int,
    offset: int,
    queries: ContentDeliveryQueries,
    content_types: ContentTypeService,
    cache: Cache | None = None,
    cache_ttl: timedelta = DEFAULT_DELIVERY_CACHE_TTL,
    links: LinkResolver | None = None,
) -> PublishedContentPage:
    """Return one page of published content, optionally filtered by type.

    With ``links``, the whole page is resolved in one batch before caching.
    """
    cache_key = f"strata:delivery:content-list:{type_key or '_all'}:{limit}:{offset}"
    if cache is not None:
        cached = cache.get(cache_key)
        if isinstance(cached, PublishedContentPage):
            return cached

    raw_items, total_count = queries.list_published(
        type_key=type_key,
        limit=limit,
        offset=offset,
    )
    documents = tuple(content_types.delivery_data(item.revision) for item in raw_items)
    if links is not None:
        documents = links.resolve_many(documents)
    items = tuple(
        _published_content(item, data)
        for item, data in zip(raw_items, documents, strict=True)
    )
    page = PublishedContentPage(
        items=items,
        total_count=total_count,
        limit=limit,
        offset=offset,
    )
    if cache is not None:
        cache.set(cache_key, page, ttl=cache_ttl)
    return page


def list_referencing_content(  # noqa: PLR0913 - keyword-only injected ports, one per dependency
    target_id: ContentId,
    *,
    source_type: ContentTypeKey | None,
    limit: int,
    offset: int,
    queries: ContentDeliveryQueries,
    sources: PublishedLinkQueries,
    index: PublishedReferenceIndex,
    content_types: ContentTypeService,
    links: LinkResolver,
) -> PublishedContentPage | None:
    """Return one page of published content referencing a published target.

    Returns ``None`` when the target itself is not published, so callers do not
    reveal whether unpublished content exists. Sources are loaded and their
    links resolved in one batch each (no per-item queries).
    """
    if queries.get_published(target_id) is None:
        return None
    source_ids, total_count = index.referencing(
        target_id, source_type=source_type, limit=limit, offset=offset
    )
    rows = sources.published_revisions(frozenset(source_ids))
    # A source unpublished between the two reads is simply omitted.
    ordered = tuple(rows[source_id] for source_id in source_ids if source_id in rows)
    documents = links.resolve_many(
        tuple(content_types.delivery_data(row.revision) for row in ordered)
    )
    return PublishedContentPage(
        items=tuple(
            _published_content(row, data)
            for row, data in zip(ordered, documents, strict=True)
        ),
        total_count=total_count,
        limit=limit,
        offset=offset,
    )


def _published_content(
    row: PublishedRevision, data: dict[str, object]
) -> PublishedContent:
    return PublishedContent(
        content_id=row.content_id,
        type_key=row.type_key,
        revision_id=row.revision.id,
        revision_number=row.revision.number,
        published_at=row.published_at,
        data=data,
    )
