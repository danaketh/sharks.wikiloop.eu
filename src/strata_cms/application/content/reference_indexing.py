"""Keep the published reference index in sync with content lifecycle events.

Driven by the outbox worker's dispatcher (at-least-once, possibly reordered),
so every relevant event reconciles the index with the source's *current*
published state instead of trusting the event payload: replaying or
reordering events converges on the same rows.
"""

from uuid import UUID

from strata_cms.application.ports.content_delivery import ContentDeliveryQueries
from strata_cms.application.ports.content_types import ContentTypeService
from strata_cms.application.ports.links import PublishedReferenceIndex
from strata_cms.application.ports.messaging import MessageEnvelope
from strata_cms.domain.value_objects import ContentId

_RELEVANT_EVENT_TYPES = frozenset(
    {
        "strata.content.published",
        "strata.content.archived",
        "strata.content.restored",
    }
)


def reindex_references_from_event(
    message: MessageEnvelope,
    *,
    queries: ContentDeliveryQueries,
    content_types: ContentTypeService,
    index: PublishedReferenceIndex,
) -> None:
    """Replace or remove one source's published reference rows; idempotent.

    Published content is (re)indexed from its published revision; content that
    is archived or not published (e.g. restored but never published) has its
    rows removed.
    """
    if message.type not in _RELEVANT_EVENT_TYPES:
        return
    content_id_raw = message.payload.get("content_id")
    if not isinstance(content_id_raw, str):
        return
    content_id = ContentId(UUID(content_id_raw))

    published = queries.get_published(content_id)
    if published is None:
        index.remove(content_id)
        return
    links = content_types.revision_links(published.revision)
    index.replace(content_id, published.revision.id, links.references)
