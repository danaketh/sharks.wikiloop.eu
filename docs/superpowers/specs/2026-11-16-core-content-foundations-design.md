# Core content foundations: rich text, media, typed references

- Date: 2026-11-16
- Status: Approved
- Project: 1 of 5 in the shark knowledge base roadmap

## Intent and why

This repository is a fresh fork of Strata that will become a shark knowledge
base (one page per species, evolution maps, attack maps, Wikipedia sync). The
public site is a separate **Next.js** app consuming the Delivery API.

Every later project needs three things Strata core lacks today:

1. **Rich text** — species articles imported from Wikipedia and edited locally.
   Project 3 will diff/merge this text git-style, so the format must be
   line-diffable.
2. **Media** — species photos, many from Wikimedia Commons, which legally
   require author/license attribution.
3. **Typed references between content items** — e.g. an attack record or an
   image used by a species page.

These are generic CMS capabilities and belong in Strata core / `plugin_api`,
not in the shark plugin.

### Roadmap context (not in scope here)

1. **This doc** — core foundations.
2. Species content type + structured sync from Wikidata with field-level
   three-way merge.
3. Article text sync with a git-like three-way merge editor.
4. Evolution maps.
5. Attack maps + attack records (data source TBD; ISAF has its own terms).

Agreed sync model for 2–3 (recorded here because it constrains this design):
Wikipedia is a *partial* source of truth; sync is manual, shows a diff, lets the
editor merge, and produces a normal **draft** revision (never auto-publishes).

## Goals

- `plugin_api` gains a Markdown rich-text field contract usable in content
  schemas and block schemas, with server-side validation of an allowed subset.
- A core `strata.media` content type: file stored via Django Storage, metadata
  revisioned like any other content, attribution fields, published-only
  Delivery.
- A Management API upload endpoint producing an immutable stored file ("blob")
  that a media revision then references.
- A `plugin_api` typed reference contract: a field declares allowed target
  content type keys; the revision stores the target `ContentId` UUID.
- A rebuildable reference index (projection) for reverse lookups
  ("what references X").
- Delivery resolves references to a small published-only summary.
- Admin editor support: Markdown editor with preview, media upload/pick,
  reference search/pick.
- A two-level validation model: **write validation** (shape/types/safety,
  every write) vs **publish readiness** (completeness rules, checked by
  `publish_content`), so imports/uploads can save incomplete drafts.
- New ADR(s) documenting the editor vocabulary extension, media, references,
  and publish readiness.

## Non-goals

- Server-side image renditions/resizing (Next.js `next/image` handles it).
- Video/audio/PDF/arbitrary file types.
- Any Wikipedia/Wikimedia fetching or sync (projects 2–3), including remote
  media import.
- Shark-specific content types.
- Delete use cases for media/blobs beyond orphan-blob cleanup.
- Full-text search changes beyond extracting text from Markdown fields.
- Translations of media metadata.

## Anti-goals (would be failure even if it "works")

- A second persistence path: no `ModelForm.save()`, no mutable media metadata
  rows edited outside revisions, no Active Record shortcuts (ADR 0001/0005).
- Delivery leaking unpublished media or unpublished reference targets.
- Raw HTML accepted anywhere in rich text; rendering that needs an HTML
  sanitizer on the frontend.
- References implemented via Django `ContentType`/`GenericForeignKey` or
  integer FKs inside revision JSON.
- Uploads trusting client file names, extensions, or declared MIME types.
- A rich-text format that cannot be line-diffed (blocks project 3).

## Constraints

- All architecture invariants in `CLAUDE.md`, `docs/ai/architecture.md`,
  `docs/ai/infrastructure.md`, ADRs 0001–0006. Import Linter must pass.
- Schemas and migrations are pure (no DB/network/storage).
- `security.md` "Files and archives" checklist applies to uploads; update the
  ASVS V12 row from "N/A".
- Defaults must work with Django FileSystemStorage + PostgreSQL only.
- `make check` and `make schema-check` warning-free.
- New runtime dependencies reviewed (maintenance, license, footprint).

## Approaches considered

**Media modeling** (the one real decision):

- **A. Separate `MediaAsset` aggregate** — own entity/repo/admin, mutable with
  version check. Simplest, but no drafts/publish (caption edits go live
  instantly), references need two target kinds, project 2 needs separate
  metadata merge logic.
- **B. Media as a content type `strata.media`** — *chosen by the user.* Reuses
  revisions, drafts, publish, Delivery rules, routing-free content identity, and
  the single reference system; project 2's merge covers image metadata. Cost:
  two-step upload (blob, then revision), new stored file per file replacement,
  orphan-blob cleanup job.
- **C. External image service (Cloudinary/imgix)** — dropped: violates
  "defaults work with Django storage", and `next/image` covers resizing.

**Rich text format** (decided in conversation):

- Restricted **CommonMark Markdown** chosen over ProseMirror/TipTap JSON
  (renders well but diffs poorly) and sanitized HTML (needs sanitizer on every
  render, noisier diffs). Next.js renders with `react-markdown` without
  raw-HTML plugins.

**References** — one sensible way: UUID in revision data + rebuildable index.

## Design

### 0. Publish readiness (decided by the user)

Drafts may be saved with required-for-publish fields empty; publishing checks
completeness.

- `ContentSchema` (and `BlockSchema`) keep `validate(value)` for **write
  validation**: shape, types, safety (e.g. Markdown subset, URL schemes). Runs
  on every create/revision write, as today.
- New optional `publish_issues(value) -> tuple[ValidationIssue, ...]`
  (default: no issues) for **publish readiness**: required fields filled,
  attribution complete, alt text present, etc. Pure, like `validate`.
- `publish_content` runs write validation **and** publish readiness on the
  revision being published (recursively for blocks) and rejects with issues if
  any. `create_revision` never runs readiness checks.
- Management API exposes readiness issues on the latest revision so the admin
  editor can show "not ready to publish: …" without attempting publish.
- Historical revisions: readiness is evaluated after in-memory migration to
  the current schema.
- Requires amending ADR 0002/0003 (or a new ADR referencing them).

### 1. Rich text (Markdown) contract

- New value type in `plugin_api`, e.g. `MarkdownText` (frozen dataclass wrapping
  `str`), plus a pure validator `validate_markdown(text, profile) ->
  tuple[ValidationIssue, ...]`.
- **Allowed subset (profile `strata.default`)**: paragraphs, headings h2–h4,
  emphasis/strong, inline code, code blocks, links, ordered/unordered lists,
  blockquotes, GFM tables, horizontal rules. **Rejected**: raw HTML (block and
  inline), images inside Markdown (use media references/blocks instead), h1
  (page title is a field), footnotes (revisit in project 3 if Wikipedia
  references need them).
- **Links**: allowed schemes `https`, `http`, `mailto`, and relative paths;
  everything else (`javascript:`, `data:`, `vbscript:` …) is a validation issue.
- Normalization on write: normalize line endings to `\n`, strip trailing
  whitespace per line, ensure single trailing newline. **Do not** reflow or
  re-render Markdown (keeps diffs stable; project 3 depends on it).
- Max length limit (builder picks a sane default, e.g. 200 KB per field).
- Parsing for validation: `markdown-it-py` (already in the lock transitively;
  becomes a direct runtime dependency) with `html` disabled; walk tokens and
  reject disallowed token types. Parser is used only for validation and text
  extraction — **stored value is the source Markdown**.
- Editor: new `EditorInputKind.MARKDOWN`. Admin renders a textarea-based
  Markdown editor with live preview (builder's choice of small, vendored,
  maintained JS editor or plain textarea + preview; no CDN). Preview rendering
  in admin must also disable raw HTML.
- Delivery: emits the Markdown string unchanged. (Optional `format: "markdown"`
  discriminator is the builder's call.)
- Search indexing: extract plain text from Markdown tokens.
- Core block: `strata.rich_text` block with one Markdown field, so block-based
  content types can use it immediately.

### 2. Media

**Blob (stored file) — infrastructure-level, not content.**

- `MediaBlobRecord` (Django persistence adapter): `id` UUID, `storage_key`
  (server-generated, e.g. `media/ab/cd/<uuid>.<ext-from-sniffed-type>`),
  `sha256`, `content_type` (sniffed), `size_bytes`, `width`, `height`,
  `created_at`, `created_by` (user id), `referenced` state for cleanup.
- Application port `MediaBlobStore` (put, open/url, delete) + repository/query
  port for blob metadata. Entities/DTOs are plain Python.
- Blobs are **immutable**: replacing a file uploads a new blob.

**Upload endpoint** — Management API `POST /api/v1/management/media/blobs/`
(multipart, single file):

- Auth: same staff + explicit permission model as other Management writes
  (new permission, e.g. `upload_media`).
- Allowlist: JPEG, PNG, WebP, GIF (static or animated accepted as-is), AVIF if
  Pillow build supports it. **SVG rejected** (script vector).
- Detect type by content (Pillow open/verify), never by client name/MIME;
  enforce max bytes (setting, default e.g. 20 MB) and max pixel count
  (decompression-bomb guard via Pillow `MAX_IMAGE_PIXELS`).
- **Metadata stripping (decided by the user): on by default.** The upload
  re-encodes the image to drop EXIF/XMP/IPTC (incl. GPS): EXIF orientation is
  applied to pixels first, ICC color profile is preserved, JPEG saved at high
  quality (≈95, no chroma subsampling change where Pillow allows), PNG/WebP/GIF
  re-saved losslessly where the format permits; animated GIF/WebP keep all
  frames. Upload request flag `preserve_original=true` (separate permission or
  same `upload_media`, builder's call) stores bytes as-is — intended for files
  whose bytes must match a source such as Commons. sha256 is computed on the
  stored bytes. Width/height read from the stored image.
- Returns `{blob_id, content_type, size_bytes, width, height, sha256}`.
- Dedup by sha256 is optional (builder's call); not required.
- New dependency: **Pillow** (needs review entry).

**`strata.media` content type** (registered by a core, installed plugin app
`strata_cms.media` using the public plugin API like any third-party plugin):

```text
MediaData (schema v1)
  blob_id: UUID               # required, must reference an existing blob
  alt_text: str               # required for publish (may be "" only if decorative=true)
  decorative: bool = False
  caption: MarkdownText | None
  title: str
  attribution:
    author: str | None
    license: str | None       # SPDX-like id or free text, e.g. "CC-BY-SA-4.0"
    license_url: str | None   # https only
    source_url: str | None    # https only
    credit_line: str | None   # verbatim credit when required
  requires_attribution: bool  # if true, author+license+source_url required
```

- Capabilities: `PUBLISHABLE`, `PREVIEWABLE`, `SEARCHABLE`. Not routable.
- `blob_id` is declared as a generic **blob field** (§2a), not media-specific
  core logic. Core validates existence on write and resolves technical data at
  Delivery time.
- Write validation vs readiness: `blob_id` required on every write;
  `alt_text` (unless `decorative`) and attribution completeness (when
  `requires_attribution`) are **publish-readiness** rules (§0), so an upload
  can create a media draft immediately.
- Delivery serializer output:

```json
{
  "url": "https://.../media/ab/cd/<uuid>.jpg",
  "content_type": "image/jpeg",
  "width": 2400, "height": 1600,
  "alt_text": "...", "decorative": false,
  "caption": "markdown...", "title": "...",
  "attribution": {...}
}
```

  Blob technical fields come from the blob query port at serialization time.
  URL comes from the storage adapter (absolute URL; served from `MEDIA_URL`
  locally, object store in prod). Next.js config must allow that host in
  `images.remotePatterns` (frontend concern, documented).
- Draft/published isolation: a blob URL is only emitted by Delivery for a
  published media revision. Blob files themselves are public-by-URL once
  uploaded (unguessable UUID key); acceptable for images and documented as a
  known property. (Private media is a non-goal.)
- **Orphan cleanup**: a task (`strata.media.cleanup_orphan_blobs`) deletes
  blobs older than a grace period (default 24 h) not referenced by any revision
  of any media content. Runs via `strata_worker`/management command; must be
  idempotent. Blobs referenced by *any* historical revision are kept (history
  stays viewable).

**2a. Generic field kinds resolved by core (builder-check call, mine).**
The media plugin must use only public contracts, but pure
`ContentDeliverySerializer`s can't call ports. So `plugin_api` gains two
**core-resolved field kinds**, declared on `ContentTypeDefinition` /
`BlockDefinition` alongside `block_fields`:

- `BlobFieldDefinition(path)` — value is a blob UUID.
- `ReferenceFieldDefinition(path, allowed_types, cardinality)` — §3.

Core (application layer, via ports) traverses declared fields on write to
validate existence/type, and on Delivery batch-resolves them and substitutes
resolved payloads into the plugin's Delivery output at the same paths. Plugin
serializers stay pure and see only raw UUIDs. No core code mentions
`strata.media`. Media summaries embedding the full media payload are produced
by the reference resolver calling the target type's Delivery output (which for
media contains the resolved blob).

**Paths** support object keys plus `[*]` for "every list item"
(e.g. `gallery[*].image`). Paths inside block data are relative to the block's
`data`; index rows record the concrete location including block UUID and list
index (e.g. `blocks/<block-uuid>/data/image`).

**Blob usage tracking**: a dedicated `MediaBlobUsageRecord(blob_id,
content_id, revision_id)` projection written in the same transaction as each
revision append (it's derived from the revision being written, so this is a
write-side index, rebuildable by command). Orphan = no usage row in **any**
revision + older than grace period. (Replaces the `referenced` state column.)

**Admin**: media list (via generic content listing), upload widget that calls
the blob endpoint then fills the working document, thumbnail preview.

### 3. Typed references

- `plugin_api` value `ContentReference` (frozen dataclass: `target: ContentId`)
  and field contract `ReferenceFieldDefinition` declared on
  `ContentTypeDefinition` / `BlockDefinition`:
  - `path` (JSON path within data, like other field declarations),
  - `allowed_types: frozenset[ContentTypeKey]`,
  - `cardinality`: single or list (with min/max).
- Revision JSON stores `{"$ref": "<uuid>"}` or plain UUID string — builder
  picks one canonical encoding and documents it; must be unambiguous for
  generic traversal (an explicit `{"ref": "<uuid>"}` object is preferred so
  generic tooling can find references without schema knowledge).
- Registry compilation: a reference to a content type owned by another plugin
  requires a declared plugin dependency (same rule as cross-plugin blocks).
  References to `strata.media` require depending on the media plugin.
- **Write-time validation** (application layer, not schema): a new port
  `ContentReferenceValidator` checks every reference target exists, is not
  archived, and its type is in `allowed_types`. Runs inside
  `create_content`/`create_revision` before append. Targets need **not** be
  published at write time (you can link drafts to drafts).
- **Reference index (projection)**: `ContentReferenceRecord(source_id,
  source_revision_id, field_path, target_id, target_type)`; maintained from
  outbox events like the search index. Two scopes:
  - *published*: rebuilt on publish/archive/restore of the source — used by
    Delivery reverse lookups;
  - *latest draft*: rebuilt on revision creation — used by admin "used by"
    panels. (Blob orphan detection uses the separate blob usage projection,
    §2a, across all revisions.)
  Includes a management command to rebuild from authoritative revisions.
- **Delivery**: reference fields serialize to a summary
  `{id, type, title?, route?}` **only if the target is currently published**,
  otherwise `null` (single) or omitted (list). Summary title source: content
  types may provide an optional summary hook (`ContentSummaryProvider` /
  `summary_title(value)`) in their definition; default is no title. For
  `strata.media`, the summary includes the media Delivery payload (url, size,
  alt, attribution) so the frontend needs no second request.
- Delivery reverse lookup endpoint (published scope only), e.g.
  `GET /api/v1/delivery/content/{id}/referenced-by/?type=...` with
  pagination. Exact URL shape is the builder's call within `/api/v1/`.
- N+1: resolution batches all reference targets of a response into one query
  port call.
- **Editor**: new `EditorInputKind.REFERENCE` (with allowed types) — admin
  renders search/pick using a Management search endpoint filtered by type;
  for `strata.media` targets the picker also offers "upload new".
- **Picker search (builder-check call, mine)**: content types that can be
  reference targets must provide a pure `summary_title(value) -> str` in their
  definition (registry compilation rejects a `ReferenceFieldDefinition` whose
  allowed type lacks it). A **latest-draft title projection**
  (`ContentTitleRecord(content_id, type, title, archived)`), updated from
  revision-created/archived/restored outbox events and rebuildable, backs a
  Management endpoint `GET /api/v1/management/content/?type=…&q=…` (staff +
  view permission, paginated). Drafts are therefore linkable.
- **Caching (builder-check call, mine)**: the existing 60 s Delivery cache
  stays; resolved references are cached with the referencing response. An
  archived/unpublished target may remain visible for up to the cache TTL —
  same as archived content today. Documented in the ADR, not a new guarantee.

### 4. ADRs and docs

- ADR 0007: Markdown rich text (format choice, subset, normalization,
  diffability rationale).
- ADR 0008: Media as content + immutable blobs.
- ADR 0009: Typed content references + reference projection.
  (May be combined if the builder prefers; ADR 0005's "deliberate extension
  contract" requirement must be explicitly satisfied.)
- Update `docs/plugins.md` with Markdown, reference, and media usage examples;
  update `security.md` ASVS V12 and A10 notes (A10 still N/A — no outbound
  fetch in this project).

## Testing expectations

- Markdown validator: allowed/disallowed constructs, link schemes, HTML
  rejection (Hypothesis for link/scheme fuzzing), normalization idempotence.
- Upload: allowlisted types accepted; SVG/HTML/polyglot/renamed files rejected;
  oversize/decompression bomb rejected; permissions; storage key never from
  client input.
- Media schema: attribution rules, alt/decorative rule, migrations scaffold.
- References: registry dependency rule; write validation (missing, archived,
  wrong type); Delivery never returns unpublished targets; batching (query
  count assertion); projection rebuild equivalence.
- Orphan cleanup idempotence and grace period (injected Clock); blob used only
  by an old revision is kept.
- Publish readiness: incomplete draft saves succeed, publish rejects with
  issues; readiness evaluated after historical migration.
- Metadata stripping: GPS EXIF absent after upload; orientation applied; ICC
  kept; `preserve_original` keeps bytes identical.
- `[*]` paths and in-block references validated, indexed and resolved.

## Left to the builder

- Exact module layout within the existing layers.
- Choice of admin Markdown editor JS (must be vendored, maintained, MIT/BSD-ish).
- Canonical JSON encoding of a reference (prefer explicit object).
- Default size limits.
- Whether to dedup blobs by hash.
- Whether to split ADRs.
- Whether `preserve_original` needs its own permission.
