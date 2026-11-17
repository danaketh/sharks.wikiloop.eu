# ADR 0009: Media as content, immutable blobs, and typed links

- Status: Accepted
- Date: 2026-11-16
- Extends: ADR 0005 (new editor input kinds)

## Context

Species pages need photos with attribution, and content items need typed
relationships to each other. Both must follow ADR 0001-0005: revisions are the
persistence unit, no mutable side models are edited outside revisions, Delivery
never leaks draft state, and plugins use only public contracts.

## Decision

### Media is a content type plus immutable blobs

- A **blob** is a stored file, infrastructure-level and not content:
  `MediaBlobRecord` (UUID, server-generated storage key, sha256, sniffed
  content type, size, width, height, creator, timestamp) behind application
  ports. Blobs are immutable; replacing a file uploads a new blob.
- `strata.media` is an ordinary content type shipped as plugin
  `strata_cms.contrib.media` using only `plugin_api`. It holds the blob link
  (`file`), title, alt text, `decorative`, caption (Markdown), attribution
  (author, license, license URL, source URL, credit line) and
  `requires_attribution`. Capabilities: publishable, previewable, searchable.
  Metadata is therefore revisioned, draft/publish isolated and mergeable like
  any other content.
- Upload: `POST /api/v1/manage/media/blobs/` (multipart, one file), requires
  staff plus the `upload_mediablobrecord` permission. It returns blob id,
  content type, size, dimensions and sha256. Creating a `strata.media` revision
  that links the blob is a separate second step, so uploads never create
  metadata outside the revision path.
- Upload safety: the type is sniffed from magic bytes against an allowlist
  (JPEG, PNG, WebP, GIF; AVIF only when Pillow supports it; SVG and everything
  else rejected) and Pillow is restricted to the sniffed decoder. Client names,
  extensions and declared MIME types are never trusted; storage keys are
  server-generated. Limits: `STRATA_MEDIA_MAX_UPLOAD_BYTES` (20 MiB, also
  pre-checked against Content-Length) and `STRATA_MEDIA_MAX_IMAGE_PIXELS`
  (50,000,000 total pixels summed across all animation frames, checked before
  decoding). Corrupt files, wrong extensions and oversize input return 400
  problem responses, never 500.
- **Metadata stripping is the default.** Images are re-encoded to drop EXIF,
  XMP, IPTC and comments, including GPS, with EXIF orientation applied to the
  pixels and the ICC profile kept. This protects editors who upload their own
  photos from leaking where they were taken. `preserve_original=true` stores the
  original bytes (every other check still runs) for files whose bytes must
  match a source such as Wikimedia Commons.
- Defaults work with `FileSystemStorage` and PostgreSQL only. `MEDIA_ROOT` and
  `MEDIA_URL` come from the environment (`DJANGO_MEDIA_ROOT`,
  `DJANGO_MEDIA_URL`). Django serves `MEDIA_URL` only when `DEBUG` is on, so
  production must serve it from the web server or an object store.
- Blob files are public by URL once uploaded (unguessable UUID key). Delivery
  only emits a blob URL for a *published* media revision. Private media is a
  non-goal.
- Orphan cleanup: `python manage.py strata_cleanup_media` deletes blobs with no
  usage row in **any** revision that are older than `STRATA_MEDIA_ORPHAN_GRACE`
  (default 24 h). It deletes the database record first, then the file, so a
  rerun after a failure converges. Blobs used by any historical revision are
  kept. **There is no worker task for this:** the repository has no scheduler or
  task registry, so operators must run the command periodically (cron or a
  scheduled container job).

### Link fields: `$ref` and `$blob`

- Plugins declare links on a `ContentTypeDefinition` or `BlockDefinition` with
  `ReferenceFieldDefinition(path, allowed_types, many, min_items, max_items)`
  and `BlobFieldDefinition(path)`. Paths are `LinkPath` values such as
  `gallery[*].image`; `[*]` means every list item. Block paths are relative to
  the block's `data`.
- Persisted encoding is an explicit object, so generic tooling can find links
  without schema knowledge: `{"$ref": "<uuid>"}` for another content item and
  `{"$blob": "<uuid>"}` for a blob. A link object at an undeclared path is a
  write validation error. `null` at a declared path means unset. IDs are
  canonical lowercase UUIDs; references never use Django `ContentType`,
  `GenericForeignKey` or integer keys.
- Registry compilation rejects a reference to a type owned by another plugin
  unless a plugin dependency is declared, and rejects a reference whose allowed
  type lacks `summary_title`.
- Core extracts links through the `ContentTypeService` port, so plugin schemas
  stay pure and see only raw UUIDs. Link extraction, validation and Delivery
  resolution never mention `strata.media`; the one exception is the Unfold
  admin, which looks up `MEDIA_CONTENT_TYPE` to offer "Upload new" on picker
  fields that accept media.
- **Write-time validation:** every link target must exist, must not be
  archived, and must be of an allowed type (blobs must exist). Targets need not
  be published, so drafts can link to drafts.

### Projections (all rebuildable)

- Draft scope, written inside the content Unit of Work in the same transaction
  as the revision append: `ContentTitleRecord` (latest-draft title, type,
  archived flag; backs the picker), draft-scope `ContentReferenceRecord` rows
  (admin "used by") and `MediaBlobUsageRecord` rows (blob usage across all
  revisions; backs orphan cleanup).
- Published scope: `ContentReferenceRecord` rows with scope `published`, kept
  current by the outbox worker from publish/archive/restore events (run
  `strata_worker --dispatcher strata_cms.config.services.dispatch_content_event`).
  The handler reconciles against the source's current published state, so
  replayed or reordered events converge. Without that worker the published
  index is not updated.
- `python manage.py strata_rebuild_projections [--scope draft|published|all]`
  truncates and refills a scope from authoritative revisions. It does **not**
  block concurrent writes: run it in a maintenance window. Links whose target no
  longer exists are skipped and listed, not fabricated. For revisions whose
  content type can no longer be decoded, blob usage is preserved by scanning the
  stored JSON for `$blob`, so history stays protected from cleanup.

### Delivery

- Reference fields resolve to a published-only summary
  `{id, type, title, path}`. The target must be *currently published*;
  otherwise the value is `null` (single) or omitted (list). A target whose
  content type is no longer installed also resolves to `null`, not an error.
  Types with `embed_in_references=True` (a generic flag; `strata.media` sets
  it) also include their Delivery payload under `data`, so the frontend needs
  no second request. Blob links resolve to a descriptor with the storage URL and
  technical metadata. All targets of a response are resolved in one batched
  query.
- `GET /api/v1/content/{id}/referenced-by/` lists published content that
  references the target (the same paginated envelope as the content list:
  `count`, `limit`, `offset`, `results`) and returns 404 when the target is
  not published.
- **Cache staleness:** Delivery keeps its 60 s TTL and no new invalidation was
  added. Resolved links are cached with the referencing response, so a
  referenced item's title or path, or an unpublished target, can remain visible
  for up to 60 s. This matches how archived content already behaves.
- The Next.js app must allow the media host in `images.remotePatterns`.

### Management and editor (the ADR 0005 extension)

ADR 0005 reserved "a deliberate future extension contract" for advanced input
needs. This ADR is that extension, kept presentation-neutral: three new
`EditorInputKind` values, `markdown`, `reference` and `blob`. `reference`
fields expose `reference_types` (allowed type keys) and `reference_many`; `blob`
fields mark an upload target. Clients may render them however they like;
validation stays in schemas and use cases.

- Picker search: `GET /api/v1/manage/content/?type=&q=&limit=&offset=` (`type`
  required, `limit` default 20 and at most 50; envelope `items`, `total_count`,
  `limit`, `offset`), backed by `ContentTitleRecord`, so drafts are linkable.
- The Unfold admin renders widgets for all three kinds; picker fields that
  accept `strata.media` offer "Upload new" through the blob endpoint.
- Known limitation: an existing reference shows `Content <uuid>` in the admin
  until it is re-picked, because the working document stores only the UUID.

## Consequences

- One reference system and one revision/draft/publish pipeline for media.
- Costs: a two-step upload, a new stored file per file replacement, an operator
  cron for orphan cleanup, a worker (or a rebuild) for the published reference
  index, and up to 60 s of staleness for resolved links.
- Archived or unpublished targets disappear from Delivery output once the cache
  expires.

## Rejected alternatives

### Separate mutable `MediaAsset` aggregate

Rejected: caption edits would go live instantly (no drafts), references would
need two target kinds, and sync would need its own metadata merge logic.

### External image service

Rejected: breaks "defaults work with Django storage", and `next/image` already
handles renditions.

### Server-side renditions

Rejected as a non-goal; the frontend resizes.

### Preserving image metadata by default

Rejected: editors' photos often embed GPS coordinates. `preserve_original`
keeps the exceptional case possible.

### Plugin schemas resolving their own links

Rejected: pure serializers cannot call ports, and per-plugin resolution would
cause N+1 queries and duplicate the draft/published rules.

### Draft projections through outbox events

Rejected: the worker is opt-in and the picker must work without it, so
draft-scope rows are written in the content Unit of Work. Only the published
scope is event-driven.

### Media-specific summary code in core

Rejected in favor of the generic `embed_in_references` flag.
