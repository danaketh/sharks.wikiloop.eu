"""Resolve ``$ref``/``$blob`` links in Delivery JSON from published state only.

References become published summaries (never draft titles or unpublished
targets); blob links become public blob descriptors. Every lookup is batched:
one published-revision query for all references, one blob query for all blobs
(including blobs inside embedded target data) and one route-path query.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from strata_cms.application.errors import ContentTypeUnavailableError
from strata_cms.application.ports.content_delivery import PublishedRevision
from strata_cms.application.ports.content_types import ContentTypeService
from strata_cms.application.ports.links import PublishedLinkQueries
from strata_cms.application.ports.media import MediaBlobRepository, MediaFileStore
from strata_cms.application.ports.routing import RoutePathProjection
from strata_cms.domain.media import MediaBlob
from strata_cms.domain.value_objects import ContentId

REF_KEY = "$ref"
BLOB_KEY = "$blob"

_LinkKind = Literal["reference", "blob"]
_Substitute = Callable[[_LinkKind, UUID | None], object]


@dataclass(frozen=True, slots=True)
class _Link:
    """One link object found in JSON; ``target`` is None when malformed."""

    kind: _LinkKind
    target: UUID | None


@dataclass(frozen=True, slots=True)
class _Target:
    """A resolvable published reference target."""

    row: PublishedRevision
    title: str | None
    data: dict[str, object] | None


class LinkResolver:
    """Replace Delivery links with published summaries and blob descriptors."""

    def __init__(
        self,
        *,
        queries: PublishedLinkQueries,
        content_types: ContentTypeService,
        blobs: MediaBlobRepository,
        files: MediaFileStore,
        routes: RoutePathProjection,
    ) -> None:
        """Bind the resolver to its published-state read ports."""
        self._queries = queries
        self._content_types = content_types
        self._blobs = blobs
        self._files = files
        self._routes = routes

    def resolve(self, data: dict[str, object]) -> dict[str, object]:
        """Return a copy of one Delivery document with its links resolved."""
        return self.resolve_many((data,))[0]

    def resolve_many(
        self, documents: Sequence[dict[str, object]]
    ) -> tuple[dict[str, object], ...]:
        """Resolve several documents sharing one batch of lookups per kind."""
        ref_ids: set[UUID] = set()
        blob_ids: set[UUID] = set()
        for document in documents:
            _collect(document, ref_ids, blob_ids)

        targets = self._load_targets(ref_ids)
        for target in targets.values():
            if target.data is not None:
                _collect(target.data, set(), blob_ids)
        blobs = self._blobs.get_many(frozenset(blob_ids)) if blob_ids else {}
        paths = self._routes.paths_for(frozenset(targets)) if targets else {}

        def blob_or_nested(kind: _LinkKind, target_id: UUID | None) -> object:
            if target_id is None:
                return None
            if kind == "blob":
                return self._descriptor(blobs.get(target_id))
            return {"id": str(target_id)}

        def substitute(kind: _LinkKind, target_id: UUID | None) -> object:
            if kind == "blob" or target_id is None:
                return blob_or_nested(kind, target_id)
            target = targets.get(ContentId(target_id))
            if target is None:
                return None
            summary: dict[str, object] = {
                "id": str(target.row.content_id),
                "type": str(target.row.type_key),
                "title": target.title,
                "path": paths.get(target.row.content_id),
            }
            if target.data is not None:
                summary["data"] = _replace(target.data, blob_or_nested)
            return summary

        return tuple(_replace(document, substitute) for document in documents)

    def _load_targets(self, ref_ids: set[UUID]) -> dict[ContentId, _Target]:
        if not ref_ids:
            return {}
        rows = self._queries.published_revisions(
            frozenset(ContentId(ref_id) for ref_id in ref_ids)
        )
        targets: dict[ContentId, _Target] = {}
        for content_id, row in rows.items():
            try:
                embeds = self._content_types.embeds_in_references(row.type_key)
                title = self._content_types.summary_title(row.revision)
                data = (
                    self._content_types.delivery_data(row.revision) if embeds else None
                )
            except ContentTypeUnavailableError:
                # The target's plugin is no longer installed: treat as missing.
                continue
            targets[content_id] = _Target(row=row, title=title, data=data)
        return targets

    def _descriptor(self, blob: MediaBlob | None) -> dict[str, object] | None:
        if blob is None:
            return None
        return {
            "url": self._files.url(blob.storage_key),
            "content_type": blob.content_type,
            "width": blob.width,
            "height": blob.height,
            "size_bytes": blob.size_bytes,
        }


def _as_link(value: Mapping[str, object]) -> _Link | None:
    if len(value) != 1:
        return None
    if REF_KEY in value:
        kind: _LinkKind = "reference"
        raw = value[REF_KEY]
    elif BLOB_KEY in value:
        kind = "blob"
        raw = value[BLOB_KEY]
    else:
        return None
    if not isinstance(raw, str):
        return _Link(kind, None)
    try:
        return _Link(kind, UUID(raw))
    except ValueError:
        return _Link(kind, None)


def _collect(value: object, ref_ids: set[UUID], blob_ids: set[UUID]) -> None:
    if isinstance(value, Mapping):
        link = _as_link(value)
        if link is not None:
            if link.target is not None:
                (ref_ids if link.kind == "reference" else blob_ids).add(link.target)
            return
        for child in value.values():
            _collect(child, ref_ids, blob_ids)
    elif isinstance(value, list):
        for child in value:
            _collect(child, ref_ids, blob_ids)


def _replace(document: dict[str, object], substitute: _Substitute) -> dict[str, object]:
    return {key: _replace_value(child, substitute) for key, child in document.items()}


def _replace_value(value: object, substitute: _Substitute) -> object:
    if isinstance(value, Mapping):
        link = _as_link(value)
        if link is not None:
            return substitute(link.kind, link.target)
        return {
            str(key): _replace_value(child, substitute) for key, child in value.items()
        }
    if isinstance(value, list):
        items: list[object] = []
        for child in value:
            if isinstance(child, Mapping) and _as_link(child) is not None:
                resolved = _replace_value(child, substitute)
                if resolved is None:
                    continue
                items.append(resolved)
            else:
                items.append(_replace_value(child, substitute))
        return items
    return value
