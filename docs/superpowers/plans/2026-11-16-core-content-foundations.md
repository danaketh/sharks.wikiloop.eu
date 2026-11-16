# Core Content Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add publish-readiness validation, Markdown rich text, typed content references, and media (immutable blobs + `strata.media` content type) to Strata core, with Delivery resolution, Management/Admin editing, and rebuildable projections.

**Architecture:** New pure contracts in `strata_cms.plugin_api` (Markdown, link fields, readiness). Core traverses declared link fields through the `ContentTypeService` port; use cases validate link targets via a query port and write draft-scope projections (titles, draft references, blob usage) inside the content Unit of Work; published-scope references are an outbox-driven projection. Media and the rich-text block ship as ordinary plugin apps under `strata_cms.contrib` using only `plugin_api`.

**Tech Stack:** Python 3.14, Django 5.2, DRF 3.18, drf-spectacular, django-unfold, markdown-it-py (new direct dep), Pillow (new dep), pytest/pytest-django/Hypothesis, Import Linter, uv.

**Spec:** `docs/superpowers/specs/2026-11-16-core-content-foundations-design.md`

## Global Constraints

- All rules in `CLAUDE.md` and `.claude/rules/*`; `make architecture-check` passes without new ignores.
- `strata_cms.domain` / `strata_cms.application` import no Django/DRF/plugin_api/infrastructure.
- New `plugin_api` modules are added to the "Public plugin core stays framework neutral" Import Linter contract.
- Schemas, migrations, readiness checks and Markdown validation are pure (no DB/network/storage).
- Dependencies via `uv add` only; never hand-edit `uv.lock`.
- Public API under `/api/v1/`; `make schema-check` warning-free; every new endpoint has `extend_schema`.
- Every new schema change ships a Django migration (`0007_…` onward); never edit applied migrations.
- Upload limits (settings, overridable): `STRATA_MEDIA_MAX_UPLOAD_BYTES = 20 * 1024 * 1024`, `STRATA_MEDIA_MAX_IMAGE_PIXELS = 50_000_000`, orphan grace `STRATA_MEDIA_ORPHAN_GRACE = timedelta(hours=24)`.
- Allowed upload types: `image/jpeg`, `image/png`, `image/webp`, `image/gif`, plus `image/avif` only when `PIL.features.check("avif")`; SVG always rejected.
- Markdown default profile: paragraphs, h2–h4, em/strong, inline code, code blocks, links, lists, blockquotes, GFM tables, hr. Rejected: raw HTML, images, h1, h5–h6, footnotes. Link schemes: `https`, `http`, `mailto`, relative. Max 200_000 characters per field.
- Link JSON encoding (builder call, fixed here): reference `{"$ref": "<uuid>"}`, blob `{"$blob": "<uuid>"}`. Any `$ref`/`$blob` object at an undeclared path is a write validation error.
- Delivery cache TTL stays 60 s; no new invalidation.
- Run `make check` at the end of every task; run `make security` after Tasks 6 and 9.
- Commit on branch `feature/core-content-foundations`.

## Deviations from the spec (decided while planning; flagged to the user)

1. Draft-scope projections (title, draft references, blob usage, archived flag) are written **inside the content UoW**, not via outbox events. The worker is opt-in, and the picker must work without it. Published-scope references remain event-driven, as in the spec.
2. Search keeps indexing raw Markdown strings (the existing `_extract_text` flattens strings; Postgres tokenization ignores Markdown punctuation). No Markdown-aware extractor.
3. Media summaries come from a generic `embed_in_references=True` flag on `ContentTypeDefinition`, not from media-specific code.

## Review Focus

1. Mixed-case or entity-encoded dangerous link schemes (`JaVaScRiPt:`, `&#106;avascript:`, `java\tscript:`) must be rejected. Test in Task 2.
2. A corrupt or truncated image, or a valid image with the wrong extension, must return a 400 problem response, never a 500. Test in Task 6.
3. A referenced target whose content type is no longer installed must resolve to `null` in Delivery, not a 500. Test in Task 7.
4. A list with 50 references must resolve in a constant number of queries. Test in Task 7.
5. A revision whose reference points to itself must save and deliver normally (the target is the source's own id). Test in Task 5.

---

### Task 1: Publish readiness

**Files:**
- Modify: `src/strata_cms/plugin_api/schema.py`, `src/strata_cms/plugin_api/definitions.py`, `src/strata_cms/plugin_api/blocks.py`, `src/strata_cms/plugin_api/registry.py`, `src/strata_cms/plugin_api/__init__.py`
- Modify: `src/strata_cms/application/ports/content_types.py`, `src/strata_cms/application/errors.py`, `src/strata_cms/application/content/use_cases.py`
- Modify: `src/strata_cms/infrastructure/content_types/registry.py`
- Modify: `src/strata_cms/api/management/views.py` (document GET + error mapping), `src/strata_cms/api/errors.py` if problem mapping lives there
- Test: `tests/plugin_api/test_readiness.py`, `tests/application/test_content_use_cases.py`, `tests/infrastructure/test_content_type_service.py`, `tests/api/test_management.py`

**Interfaces:**
- Produces:
  - `plugin_api.schema.PublishReadiness[T]` (Protocol): `def publish_issues(self, value: T) -> tuple[ValidationIssue, ...]`
  - `ContentTypeDefinition.readiness: PublishReadiness[T] | None = None`; `ContentTypeDefinition.publish_issues_untyped(value: object) -> tuple[ValidationIssue, ...]`
  - `BlockDefinition.readiness: PublishReadiness[T] | None = None`; `BlockDefinition.publish_issues_untyped(value: object) -> tuple[ValidationIssue, ...]`
  - `BlockTypeRegistry.publish_issues(data: RevisionData, fields: tuple[BlockFieldDefinition, ...]) -> tuple[ValidationIssue, ...]` (recursive; issue paths prefixed like write validation, e.g. `$.body[0].data.text`)
  - `ContentTypeService.publish_issues(revision: Revision) -> tuple[ContentDataProblem, ...]`
  - `application.errors.ContentNotReadyError(ApplicationError)` with `.type_key` and `.problems: tuple[ContentDataProblem, ...]`
  - Management document GET adds `"publish_readiness": {"ready": bool, "problems": [{"code","message","path"}]}` for the latest revision.

- [ ] **Step 1: Write failing tests**

```python
# tests/plugin_api/test_readiness.py
def test_definition_without_readiness_reports_no_issues(): ...
    # publish_issues_untyped(value) == ()
def test_definition_readiness_issues_are_returned_unchanged(): ...
def test_block_registry_collects_nested_block_readiness_with_prefixed_paths():
    # issue.path == "$.body[0].slots.children[1].data.text"

# tests/application/test_content_use_cases.py
def test_create_revision_succeeds_when_readiness_has_issues(): ...
def test_publish_rejects_with_content_not_ready_and_does_not_publish():
    # raises ContentNotReadyError; problems[0].code == "<fake code>"; no event published; content.published_revision_id unchanged
def test_publish_evaluates_readiness_after_historical_migration(): ...

# tests/api/test_management.py
def test_publish_not_ready_returns_422_with_problems(): ...
    # status 422; body problems list has code/path
def test_document_includes_publish_readiness_for_latest_revision(): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/plugin_api/test_readiness.py tests/application/test_content_use_cases.py -k readiness -v` → FAIL (missing attributes).
- [ ] **Step 3: Implement.** `publish_content` calls `content_types.validate_revision(revision)` then `content_types.publish_issues(revision)` and raises `ContentNotReadyError` if non-empty, before `content.publish`. Readiness runs on the decoded current-schema value (after migration and block normalization). Management maps `ContentNotReadyError` to 422 using the same problem shape as `InvalidContentDataError`.
- [ ] **Step 4: Run** the step-2 tests plus `uv run pytest tests -q` → PASS.
- [ ] **Step 5:** `make check` → PASS. Commit: `git commit -m "Add publish-readiness validation separate from write validation"`

---

### Task 2: Markdown rich text contract and `strata.rich_text` block

**Files:**
- Create: `src/strata_cms/plugin_api/markdown.py`
- Modify: `src/strata_cms/plugin_api/editor.py` (`EditorInputKind.MARKDOWN = "markdown"`), `src/strata_cms/plugin_api/__init__.py`, `pyproject.toml` (Import Linter list + dependency)
- Create: `src/strata_cms/contrib/__init__.py`, `src/strata_cms/contrib/rich_text/__init__.py`, `src/strata_cms/contrib/rich_text/apps.py`, `src/strata_cms/contrib/rich_text/blocks.py`
- Modify: `src/strata_cms/config/settings/base.py` (INSTALLED_APPS adds `strata_cms.contrib.rich_text.apps.RichTextPluginConfig`)
- Test: `tests/plugin_api/test_markdown.py`, `tests/contrib/test_rich_text_block.py`

**Interfaces:**
- Produces (`strata_cms.plugin_api.markdown`, re-exported from `plugin_api`):
  - `MarkdownText` frozen dataclass `(source: str)`
  - `MarkdownProfile` frozen dataclass `(allowed_heading_levels: frozenset[int], allowed_link_schemes: frozenset[str], allow_relative_links: bool, allow_tables: bool, max_length: int)`; `DEFAULT_MARKDOWN_PROFILE` with Global Constraints values
  - `normalize_markdown(source: str) -> str`: CRLF/CR→LF, rstrip each line, exactly one trailing `\n` (empty input stays `""`)
  - `validate_markdown(source: str, *, profile: MarkdownProfile = DEFAULT_MARKDOWN_PROFILE, path: str = "$") -> tuple[ValidationIssue, ...]`. Issue codes: `markdown_too_long`, `markdown_html_not_allowed`, `markdown_image_not_allowed`, `markdown_heading_level_not_allowed`, `markdown_construct_not_allowed`, `markdown_link_scheme_not_allowed`
  - `render_markdown_preview(source: str) -> str`: HTML for admin preview only (markdown-it `commonmark` + table, `html=False`)
  - `RICH_TEXT_BLOCK_TYPE = BlockTypeKey("strata.rich_text")`, block data `{"text": "<markdown>"}`, plugin key `strata.rich_text`

- [ ] **Step 1: Add dependency.** `uv add "markdown-it-py>=4.2,<5"` → `uv.lock` updated.
- [ ] **Step 2: Write failing tests**

```python
# tests/plugin_api/test_markdown.py
@pytest.mark.parametrize("src", ["## Hi\n", "*a* **b** `c`\n", "- x\n- y\n", "> q\n",
    "| a | b |\n|---|---|\n| 1 | 2 |\n", "[x](https://e.org)\n", "[x](/species/mako)\n",
    "[m](mailto:a@b.cz)\n", "```\ncode\n```\n", "---\n"])
def test_allowed_constructs_have_no_issues(src): assert validate_markdown(src) == ()

@pytest.mark.parametrize(("src", "code"), [
    ("<b>x</b>\n", "markdown_html_not_allowed"),
    ("x <span>y</span>\n", "markdown_html_not_allowed"),
    ("![a](https://e.org/a.png)\n", "markdown_image_not_allowed"),
    ("# H1\n", "markdown_heading_level_not_allowed"),
    ("##### H5\n", "markdown_heading_level_not_allowed"),
    ("[x](javascript:alert(1))\n", "markdown_link_scheme_not_allowed"),
    ("[x](JaVaScRiPt:alert(1))\n", "markdown_link_scheme_not_allowed"),
    ("[x](&#106;avascript:alert(1))\n", "markdown_link_scheme_not_allowed"),
    ("[x](data:text/html,hi)\n", "markdown_link_scheme_not_allowed"),
    ("<javascript:alert(1)>\n", "markdown_link_scheme_not_allowed"),
])
def test_disallowed_constructs(src, code): assert code in {i.code for i in validate_markdown(src)}

def test_too_long_is_rejected(): ...  # "a" * 200_001 -> markdown_too_long
def test_issue_paths_use_given_prefix(): ...  # path="$.body" -> issue.path == "$.body"
@given(st.text())
def test_normalize_is_idempotent(s): assert normalize_markdown(normalize_markdown(s)) == normalize_markdown(s)
def test_normalize_does_not_reflow(): assert normalize_markdown("a\nb  \r\n") == "a\nb\n"
@given(st.text())
def test_preview_never_emits_raw_html_tags_from_input(s):
    assert "<script" not in render_markdown_preview(s).lower()

# tests/contrib/test_rich_text_block.py
def test_rich_text_block_normalizes_text_on_write(): ...  # "a  \r\n" stored as "a\n"
def test_rich_text_block_rejects_html(): ...  # BlockValidationError code markdown_html_not_allowed at "$...data.text"
def test_rich_text_block_registered_with_markdown_editor(): ...  # editor field input_kind == EditorInputKind.MARKDOWN
```

- [ ] **Step 3: Run** `uv run pytest tests/plugin_api/test_markdown.py tests/contrib -v` → FAIL (import errors).
- [ ] **Step 4: Implement.** Validation walks `MarkdownIt("commonmark", {"html": True}).enable("table")` tokens with `html=True` so HTML becomes detectable `html_block`/`html_inline` tokens, which are then rejected. Link hrefs are checked after markdown-it's own normalization (it decodes entities), lowercased, with `\t\n\r` and control characters stripped. Footnote, strikethrough and other plugins stay disabled (they parse as text). The schema's `deserialize` calls `normalize_markdown`; `validate` calls `validate_markdown`.
- [ ] **Step 5: Run** the step-3 tests → PASS. `make check` → PASS.
- [ ] **Step 6: Commit** `git commit -m "Add Markdown rich-text contract and core rich_text block"`

---

### Task 3: Link field contracts (references, blobs) and registry compilation rules

**Files:**
- Create: `src/strata_cms/plugin_api/links.py`
- Modify: `src/strata_cms/plugin_api/definitions.py`, `src/strata_cms/plugin_api/blocks.py`, `src/strata_cms/plugin_api/registry.py`, `src/strata_cms/plugin_api/editor.py`, `src/strata_cms/plugin_api/errors.py`, `src/strata_cms/plugin_api/__init__.py`, `pyproject.toml` (Import Linter list)
- Test: `tests/plugin_api/test_links.py`, `tests/plugin_api/test_registry.py`

**Interfaces:**
- Produces (`strata_cms.plugin_api.links`):
  - `LinkPath` frozen dataclass `(segments: tuple[str, ...])`, `LinkPath.parse("gallery[*].image")` → segments `("gallery", "*", "image")`; `[*]` is allowed after any segment, and nested wildcards are allowed; invalid → `RegistryConfigurationError`
  - `ReferenceFieldDefinition(path: LinkPath, allowed_types: frozenset[ContentTypeKey], many: bool = False, min_items: int = 0, max_items: int | None = None)`
  - `BlobFieldDefinition(path: LinkPath)`
  - `ContentReference(target: UUID)` with `to_json() -> {"$ref": str}` and `ContentReference.from_json(raw: object) -> ContentReference` (raises `SchemaDecodeError`)
  - `BlobReference(blob_id: UUID)` with `to_json() -> {"$blob": str}` / `from_json`
  - `LinkOccurrence(kind: Literal["reference","blob"], location: str, target: UUID)` where `location` is a concrete path, e.g. `gallery/2/image` or `body/blocks/<block-uuid>/data/image`
  - `iter_link_values(data: Mapping[str, object], path: LinkPath) -> Iterator[tuple[str, object]]` (concrete location, raw value; missing keys yield nothing)
  - `find_undeclared_links(data: Mapping[str, object], declared: Iterable[LinkPath]) -> tuple[str, ...]` (locations of `$ref`/`$blob` objects not matched by a declared path)
- `ContentTypeDefinition` gains: `reference_fields: tuple[ReferenceFieldDefinition, ...] = ()`, `blob_fields: tuple[BlobFieldDefinition, ...] = ()`, `summary_title: Callable[[T], str] | None = None`, `embed_in_references: bool = False`; method `summary_title_untyped(value: object) -> str | None`
- `BlockDefinition` gains `reference_fields`, `blob_fields` (paths relative to block `data`)
- `BlockTypeRegistry.collect_links(data: RevisionData, fields: tuple[BlockFieldDefinition, ...]) -> tuple[LinkOccurrence, ...]`
- `RegisteredContentType` protocol is extended with the new attributes
- `EditorInputKind.REFERENCE = "reference"`, `EditorInputKind.BLOB = "blob"`; `ScalarEditorField.reference_types: tuple[ContentTypeKey, ...] = ()`, which is required to be non-empty exactly when `input_kind is REFERENCE`
- Registry compile errors: `UnknownReferenceTargetTypeError`, `ReferenceTargetWithoutTitleError` (target type lacks `summary_title`), and the existing `UndeclaredPluginDependencyError` for cross-plugin target types

- [ ] **Step 1: Write failing tests**

```python
# tests/plugin_api/test_links.py
def test_parse_wildcard_path(): assert LinkPath.parse("gallery[*].image").segments == ("gallery", "*", "image")
@pytest.mark.parametrize("bad", ["", "a..b", "a[0]", "a[*", "1a", "a.[*]"])
def test_parse_rejects_invalid(bad): ...
def test_iter_link_values_expands_lists_with_concrete_locations():
    data = {"gallery": [{"image": {"$blob": U1}}, {"image": {"$blob": U2}}]}
    assert [loc for loc, _ in iter_link_values(data, LinkPath.parse("gallery[*].image"))] == ["gallery/0/image", "gallery/1/image"]
def test_reference_round_trip(): assert ContentReference.from_json(ContentReference(U1).to_json()).target == U1
@pytest.mark.parametrize("raw", [U1_STR, {"$ref": "nope"}, {"$ref": U1_STR, "x": 1}, {"ref": U1_STR}])
def test_reference_from_json_rejects(raw): ...
def test_find_undeclared_links_reports_extra_ref(): ...  # {"a": {"$ref": U1}} with no declared paths -> ("a",)

# tests/plugin_api/test_registry.py
def test_reference_to_unregistered_type_fails_compile(): ...
def test_reference_to_type_without_summary_title_fails_compile(): ...
def test_reference_to_other_plugin_type_requires_dependency(): ...
def test_block_links_collected_with_block_uuid_location():
    # location == f"body/blocks/{block_id}/data/image"
def test_reference_editor_field_requires_reference_types(): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/plugin_api/test_links.py tests/plugin_api/test_registry.py -v` → FAIL.
- [ ] **Step 3: Implement** the contracts and registry rules. The plugin-dependency check reuses `_transitive_dependencies`, applied to the owners of content types instead of blocks.
- [ ] **Step 4: Run** → PASS. `make check` → PASS.
- [ ] **Step 5: Commit** `git commit -m "Add typed reference and blob link field contracts"`

---

### Task 4: Link extraction through `ContentTypeService`

**Files:**
- Modify: `src/strata_cms/application/ports/content_types.py`, `src/strata_cms/infrastructure/content_types/registry.py`
- Test: `tests/infrastructure/test_content_type_service.py`

**Interfaces:**
- Consumes: Task 3 `LinkOccurrence`, `iter_link_values`, `find_undeclared_links`, `BlockTypeRegistry.collect_links`
- Produces (application port types, plain dataclasses, no plugin_api imports):
  - `ContentLink(kind: str, location: str, target: UUID, allowed_types: frozenset[ContentTypeKey])`; `allowed_types` is empty for blobs
  - `ContentLinks(references: tuple[ContentLink, ...], blobs: tuple[ContentLink, ...])`
  - `ContentTypeService.extract_links(*, type_key: ContentTypeKey, data: RevisionData) -> ContentLinks` (input is canonical current-version data from `prepare_for_write`)
  - `ContentTypeService.summary_title(revision: Revision) -> str | None`
  - `ContentTypeService.embeds_in_references(type_key: ContentTypeKey) -> bool`
  - `ContentTypeService.blob_ids(revision: Revision) -> frozenset[UUID]` (used by the projection rebuild)
- `prepare_for_write` additionally raises `InvalidContentDataError` with code `undeclared_link` at each undeclared `$ref`/`$blob` location. It also checks cardinality: problem code `reference_count_out_of_range`, using `min_items`/`max_items` for `many=True` fields.

- [ ] **Step 1: Write failing tests** `test_extract_links_from_fields_and_blocks`, `test_extract_links_carries_allowed_types`, `test_prepare_rejects_undeclared_ref`, `test_prepare_rejects_reference_count_out_of_range`, `test_summary_title_none_when_type_has_no_hook`.
- [ ] **Step 2: Run** `uv run pytest tests/infrastructure/test_content_type_service.py -v` → FAIL.
- [ ] **Step 3: Implement** in `RegistryContentTypeService`, converting `plugin_api` types to the port dataclasses.
- [ ] **Step 4: Run** → PASS; `make check` → PASS.
- [ ] **Step 5: Commit** `git commit -m "Expose link extraction and summary titles via ContentTypeService"`

---

### Task 5: Write-time link validation and draft-scope projections

**Files:**
- Create: `src/strata_cms/application/ports/links.py`
- Modify: `src/strata_cms/application/ports/content_uow.py`, `src/strata_cms/application/content/use_cases.py`
- Modify: `src/strata_cms/infrastructure/persistence/django/models.py`, `.../unit_of_work.py`
- Create: `src/strata_cms/infrastructure/persistence/django/projections.py`, migration `0007_link_projections_and_media_blobs.py`
- Test: `tests/application/test_content_use_cases.py`, `tests/infrastructure/test_link_projections.py`

**Interfaces:**
- Consumes: Task 4 `ContentTypeService.extract_links`, `summary_title`
- Produces:
  - `application.ports.links.LinkTargetQueries` (Protocol):
    - `content_targets(ids: frozenset[ContentId]) -> Mapping[ContentId, LinkTarget]`
    - `existing_blobs(ids: frozenset[UUID]) -> frozenset[UUID]`
  - `LinkTarget(content_id: ContentId, type_key: ContentTypeKey, archived: bool)`
  - `ContentProjectionWriter` (Protocol, exposed as `ContentUnitOfWork.projections`):
    - `record_revision(*, content_id: ContentId, revision_id: RevisionId, type_key: ContentTypeKey, title: str | None, links: ContentLinks) -> None`: upserts the title, replaces the draft-scope reference rows for the content, and appends blob usage rows for the revision
    - `set_archived(content_id: ContentId, archived: bool) -> None`
  - `create_content` / `create_revision` gain a keyword-only parameter `link_targets: LinkTargetQueries`; `archive_content` / `restore_content` call `uow.projections.set_archived`
  - Problem codes: `reference_not_found`, `reference_archived`, `reference_type_not_allowed`, `blob_not_found` (path = `$.` + location with `/` → `.`)
  - Records (all `db_table` prefixed `strata_`):
    - `ContentTitleRecord(content_id UUID pk, type_key, title, archived bool, updated_at)` with index `(type_key, title)`
    - `ContentReferenceRecord(id bigauto, scope ["draft","published"], source_id UUID, source_revision_id UUID, location, target_id UUID, target_type)` with indexes `(scope, target_id)` and `(scope, source_id)`
    - `MediaBlobRecord(id UUID pk, storage_key unique, sha256, content_type, size_bytes, width, height, created_at, created_by)`, Meta permission `upload_mediablobrecord`
    - `MediaBlobUsageRecord(blob_id UUID, content_id UUID, revision_id UUID)` with unique constraint `(blob_id, revision_id)`
- Self-reference rule: a reference whose target equals the source content id is valid. On `create_content` the id is new, so the target cannot exist yet; that case returns `reference_not_found`.

- [ ] **Step 1: Write failing tests**

```python
# application (fakes for LinkTargetQueries and projections)
def test_create_revision_rejects_missing_reference_target(): ...  # code reference_not_found, path "$.species"
def test_create_revision_rejects_archived_target(): ...
def test_create_revision_rejects_wrong_target_type(): ...
def test_create_revision_rejects_missing_blob(): ...
def test_create_revision_allows_unpublished_target(): ...
def test_revision_may_reference_its_own_content(): ...  # Review Focus 5
def test_create_revision_records_projection_with_title_and_links(): ...
def test_archive_marks_title_archived(): ...

# tests/infrastructure/test_link_projections.py (django_db)
def test_draft_references_replaced_on_new_revision(): ...
def test_blob_usage_kept_for_all_revisions(): ...
def test_projection_write_rolls_back_with_failed_uow(): ...
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Validation runs after `prepare_for_write` and before entering the UoW for create, and inside the UoW for revision (after the version check). Wire `DjangoLinkTargetQueries` in `config/services.py` (`get_link_target_queries()`) and pass it from every caller of the use cases (API views, admin). `uv run python manage.py makemigrations strata_persistence --name link_projections_and_media_blobs`.
- [ ] **Step 4: Run** → PASS; `make check` → PASS (migration check included).
- [ ] **Step 5: Commit** `git commit -m "Validate link targets on write and maintain draft-scope projections"`

---

### Task 6: Media blob upload (port, Pillow processing, endpoint)

**Files:**
- Create: `src/strata_cms/domain/media.py`, `src/strata_cms/application/ports/media.py`, `src/strata_cms/application/media/__init__.py`, `src/strata_cms/application/media/use_cases.py`
- Create: `src/strata_cms/infrastructure/media/__init__.py`, `src/strata_cms/infrastructure/media/pillow.py`, `src/strata_cms/infrastructure/media/storage.py`, `src/strata_cms/infrastructure/persistence/django/media.py`
- Modify: `src/strata_cms/application/ports/authorization.py` (`ContentAction.UPLOAD_MEDIA`), `src/strata_cms/infrastructure/authorization/policy.py` (maps to `strata_persistence.upload_mediablobrecord`), `src/strata_cms/config/services.py`, `src/strata_cms/config/settings/base.py`, `src/strata_cms/api/management/urls.py`, `src/strata_cms/api/management/views.py`, `pyproject.toml`
- Test: `tests/infrastructure/test_pillow_processor.py`, `tests/application/test_media_use_cases.py`, `tests/api/test_media_upload.py`

**Interfaces:**
- Produces:
  - `domain.media.MediaBlob(id: UUID, storage_key: str, sha256: str, content_type: str, size_bytes: int, width: int, height: int, created_at: datetime, created_by: ActorId)`
  - `ports.media.ImageProcessor.process(data: bytes, *, preserve_original: bool) -> ProcessedImage` raising `UnsupportedMediaError(reason: str)`; `ProcessedImage(data: bytes, content_type: str, extension: str, width: int, height: int)`
  - `ports.media.MediaFileStore`: `save(key: str, data: bytes) -> None`, `url(key: str) -> str`, `delete(key: str) -> None`
  - `ports.media.MediaBlobRepository`: `add(blob: MediaBlob) -> None`, `get_many(ids: frozenset[UUID]) -> Mapping[UUID, MediaBlob]`, `list_orphans(*, created_before: datetime, limit: int) -> tuple[MediaBlob, ...]`, `delete(blob_id: UUID) -> None`
  - `application.media.use_cases.upload_media_blob(command: UploadMediaBlob, *, processor, files, blobs, ids, clock, policy, actor, max_bytes: int) -> MediaBlob`; `UploadMediaBlob(actor_id: ActorId, data: bytes, preserve_original: bool)`
  - Storage key: `media/{hex[0:2]}/{hex[2:4]}/{uuid}.{extension}`; the extension always comes from the sniffed type
  - Endpoint `POST /api/v1/management/media/blobs/` (multipart, field `file`, optional `preserve_original` boolean). 201 → `{"blob_id","content_type","size_bytes","width","height","sha256"}`; 400 problem with `code` in `{"media_too_large","media_type_not_allowed","media_unreadable","media_too_many_pixels"}`; 403 without permission. `preserve_original` uses the same permission (builder call).
  - Metadata stripping (default): apply `ImageOps.exif_transpose`; save without `exif`/`xmp`; keep `icc_profile`; JPEG `quality=95, subsampling="keep"` when the source is JPEG; PNG/WebP lossless (`lossless=True` for WebP only if the source was lossless; otherwise `quality=95`); GIF/WebP animations keep all frames (`save_all=True`, durations/loop preserved).

- [ ] **Step 1: Add dependency** `uv add "pillow>=11.3,<13"`.
- [ ] **Step 2: Write failing tests** (generate fixtures in-test with Pillow; no binary files in repo)

```python
# tests/infrastructure/test_pillow_processor.py
def test_jpeg_gps_exif_is_stripped(): ...  # processed has no 0x8825 GPS IFD in getexif()
def test_orientation_applied_before_strip(): ...  # 100x50 with Orientation=6 -> width 50, height 100
def test_icc_profile_preserved(): ...
def test_preserve_original_returns_identical_bytes(): ...
def test_animated_gif_keeps_frames(): ...  # n_frames == 3
def test_svg_rejected(): ...  # UnsupportedMediaError reason "media_type_not_allowed"
def test_png_renamed_as_jpg_detected_as_png(): ...  # content_type == "image/png"
def test_truncated_jpeg_rejected(): ...  # reason "media_unreadable"  (Review Focus 2)
def test_html_polyglot_rejected(): ...  # b"<html>..." -> media_unreadable
def test_decompression_bomb_rejected(): ...  # header claiming 20000x20000 -> media_too_many_pixels

# tests/api/test_media_upload.py
def test_upload_returns_201_and_server_generated_key(): ...  # storage key ignores client filename "../../evil.php"
def test_upload_over_limit_returns_400_media_too_large(): ...
def test_upload_corrupt_returns_400_not_500(): ...
def test_upload_requires_permission(): ...  # staff without upload perm -> 403
def test_upload_requires_authentication(): ...
```

- [ ] **Step 3: Run** `uv run pytest tests/infrastructure/test_pillow_processor.py tests/application/test_media_use_cases.py tests/api/test_media_upload.py -v` → FAIL.
- [ ] **Step 4: Implement.** Size check happens before decoding (DRF `request.FILES` read with limit `max_bytes + 1`). Set `Image.MAX_IMAGE_PIXELS` per call through a local check of `width*height` before `load()`, not a process-global mutation. File saved first, then the blob record. If the record write raises, delete the file and re-raise. `MediaFileStore` default wraps `django.core.files.storage.default_storage`.
- [ ] **Step 5: Run** → PASS; `make check` → PASS; `make security` → PASS.
- [ ] **Step 6: Commit** `git commit -m "Add media blob upload with type sniffing and metadata stripping"`

---

### Task 7: Delivery link resolution

**Files:**
- Create: `src/strata_cms/application/content/links.py`
- Modify: `src/strata_cms/application/ports/links.py`, `src/strata_cms/application/ports/routing.py` (`RoutePathProjection.paths_for(ids: frozenset[ContentId]) -> Mapping[ContentId, str]`), `src/strata_cms/application/content/delivery.py`
- Modify: `src/strata_cms/infrastructure/persistence/django/queries.py`, `.../routing.py`, `src/strata_cms/config/services.py`, `src/strata_cms/api/delivery/views.py`
- Test: `tests/application/test_link_resolution.py`, `tests/api/test_delivery.py`

**Interfaces:**
- Consumes: `ContentTypeService.delivery_data`, `summary_title`, `embeds_in_references`; `MediaBlobRepository.get_many`; `MediaFileStore.url`; `ContentDeliveryQueries`
- Produces:
  - `ports.links.PublishedLinkQueries.published_revisions(ids: frozenset[ContentId]) -> Mapping[ContentId, PublishedRevisionRow]` (one query; reuses the existing published DTO shape)
  - `application.content.links.LinkResolver(queries, content_types, blobs, files, routes)` with `resolve(data: dict[str, object]) -> dict[str, object]`
    - Collects every `{"$ref": id}` / `{"$blob": id}` in Delivery JSON, batch-loads them (one call per kind), and substitutes:
    - `$ref` → `{"id","type","title","path"}` plus `"data"` (the target's Delivery data with blobs resolved and nested refs replaced by `{"id": ...}` only) when `embeds_in_references`; `null` if the target is unpublished, archived, missing or of an uninstalled type; list items that resolve to `null` are removed
    - `$blob` → `{"url","content_type","width","height","size_bytes"}`, or `null` if missing
  - `get_published_content` / `list_published_content` gain a keyword-only `links: LinkResolver | None = None`; resolution happens before caching (resolved output is what gets cached)

- [ ] **Step 1: Write failing tests**

```python
def test_published_target_resolves_to_summary_with_title_and_path(): ...
def test_unpublished_target_resolves_to_null(): ...
def test_archived_target_list_item_removed(): ...
def test_uninstalled_target_type_resolves_to_null(): ...  # Review Focus 3
def test_embedded_target_includes_resolved_blob(): ...  # data.file.url endswith ".jpg"
def test_nested_refs_in_embedded_data_not_expanded(): ...
def test_fifty_references_constant_queries(django_assert_max_num_queries): ...  # Review Focus 4; <= 6 queries
def test_delivery_endpoint_never_returns_draft_target_title(): ...
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**; wire `get_link_resolver()` in `config/services.py` and pass it from the Delivery views.
- [ ] **Step 4: Run** → PASS; `make check` → PASS.
- [ ] **Step 5: Commit** `git commit -m "Resolve references and blobs in Delivery responses"`

---

### Task 8: Published reference index, reverse lookup, rebuild command

**Files:**
- Create: `src/strata_cms/application/content/reference_indexing.py`
- Modify: `src/strata_cms/application/ports/links.py`, `src/strata_cms/config/services.py` (`dispatch_content_event` calls search **and** reference reindexing)
- Create: `src/strata_cms/infrastructure/persistence/django/management/commands/strata_rebuild_projections.py`
- Modify: `src/strata_cms/api/delivery/urls.py`, `src/strata_cms/api/delivery/views.py`, `src/strata_cms/api/delivery/serializers.py`
- Test: `tests/application/test_reference_indexing.py`, `tests/api/test_delivery.py`, `tests/infrastructure/test_rebuild_projections.py`

**Interfaces:**
- Produces:
  - `ports.links.PublishedReferenceIndex`:
    - `replace(source_id: ContentId, revision_id: RevisionId, links: tuple[ContentLink, ...]) -> None`
    - `remove(source_id: ContentId) -> None`
    - `referencing(target_id: ContentId, *, source_type: ContentTypeKey | None, limit: int, offset: int) -> tuple[tuple[ContentId, ...], int]`
  - `reindex_references_from_event(message: MessageEnvelope, *, queries: ContentDeliveryQueries, content_types: ContentTypeService, index: PublishedReferenceIndex) -> None` handling `strata.content.published|archived|restored`; idempotent
  - `GET /api/v1/delivery/content/{content_id}/referenced-by/?type=&limit=&offset=` → `{"items": [<same shape as list_published_content items>], "total_count", "limit", "offset"}`; sources not currently published are excluded at query time
  - `python manage.py strata_rebuild_projections [--scope draft|published|all]`: rebuilds titles, draft references and blob usage from all revisions, and published references from published revisions. It truncates and refills inside one transaction per scope.

- [ ] **Step 1: Write failing tests** `test_publish_event_indexes_references`, `test_archive_event_removes_rows`, `test_event_handling_is_idempotent`, `test_referenced_by_lists_only_published_sources`, `test_referenced_by_filters_by_type`, `test_rebuild_reproduces_incrementally_built_state` (build via use cases, snapshot rows, rebuild, compare).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; `make check` → PASS.
- [ ] **Step 5: Commit** `git commit -m "Add published reference index, referenced-by endpoint and projection rebuild"`

---

### Task 9: `strata.media` content type and orphan cleanup

**Files:**
- Create: `src/strata_cms/contrib/media/__init__.py`, `apps.py`, `schema.py`
- Create: `src/strata_cms/application/media/cleanup.py`, `src/strata_cms/infrastructure/persistence/django/management/commands/strata_cleanup_media.py`
- Modify: `src/strata_cms/config/settings/base.py` (INSTALLED_APPS adds `strata_cms.contrib.media.apps.MediaPluginConfig`; `MEDIA_ROOT`/`MEDIA_URL` defaults from env), `src/strata_cms/config/urls.py` (serve `MEDIA_URL` only when `DEBUG`)
- Test: `tests/contrib/test_media_type.py`, `tests/application/test_media_cleanup.py`, `tests/api/test_media_flow.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 3, 6, 7
- Produces:
  - Plugin key `strata.media`; content type `ContentTypeKey("strata.media")`, `schema_version=1`, capabilities `{PUBLISHABLE, PREVIEWABLE, SEARCHABLE}`, `blob_fields=(BlobFieldDefinition(LinkPath.parse("file")),)`, `embed_in_references=True`, `summary_title = title or alt_text or "Untitled media"`
  - `MediaData(file: BlobReference, title: str, alt_text: str, decorative: bool, caption: MarkdownText | None, attribution: Attribution, requires_attribution: bool)`; `Attribution(author, license, license_url, source_url, credit_line)`, all `str | None`
  - Write validation: `file` required; `license_url`/`source_url` must be `https` (`attribution_url_not_https`); caption passes `validate_markdown`
  - Readiness: `alt_text_required` (non-decorative with blank alt), `attribution_incomplete` (requires_attribution and any of author/license/source_url blank)
  - Editor: fields `file` (BLOB), `title`, `alt_text`, `decorative` (BOOLEAN), `caption` (MARKDOWN), the attribution fields, `requires_attribution`
  - `cleanup_orphan_blobs(*, blobs: MediaBlobRepository, files: MediaFileStore, clock: Clock, grace: timedelta, batch_size: int = 100) -> int`: deletes the record first, then the file; a missing file is not an error, so a rerun is idempotent
  - `MediaBlobRepository.list_orphans` = no `MediaBlobUsageRecord` row and `created_at < now - grace`

- [ ] **Step 1: Write failing tests**

```python
def test_media_draft_saves_without_alt_text(): ...
def test_media_publish_rejected_without_alt_text(): ...  # ContentNotReadyError code alt_text_required
def test_decorative_media_publishes_without_alt(): ...
def test_requires_attribution_blocks_publish_until_complete(): ...  # code attribution_incomplete
def test_http_source_url_rejected_on_write(): ...
def test_media_delivery_payload_includes_url_dimensions_and_attribution(): ...
def test_cleanup_deletes_only_unused_blobs_past_grace(): ...  # injected Clock
def test_cleanup_keeps_blob_used_only_by_old_revision(): ...
def test_cleanup_is_idempotent_when_file_already_gone(): ...
def test_end_to_end_upload_create_publish_reference_deliver(): ...  # API-level
```

- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; `make check` → PASS; `make security` → PASS.
- [ ] **Step 5: Commit** `git commit -m "Add strata.media content type and orphan blob cleanup"`

---

### Task 10: Management picker search and editor contract

**Files:**
- Create: `src/strata_cms/application/content/picker.py`
- Modify: `src/strata_cms/application/ports/content_queries.py`, `src/strata_cms/infrastructure/persistence/django/queries.py`, `src/strata_cms/infrastructure/editors/registry.py`, `src/strata_cms/application/editor_models.py`, `src/strata_cms/api/management/views.py`
- Create: `src/strata_cms/api/management/markdown.py` (preview view), with a modification to `src/strata_cms/api/management/urls.py`
- Test: `tests/api/test_management.py`, `tests/application/test_picker.py`

**Interfaces:**
- Produces:
  - `ContentEditorQueries.search_titles(*, type_keys: frozenset[ContentTypeKey], query: str, limit: int, offset: int) -> tuple[tuple[ContentTitleRow, ...], int]` with `ContentTitleRow(content_id, type_key, title, is_published: bool)`; excludes archived; case-insensitive `icontains`; `query` is stripped and capped at 200 chars
  - `GET /api/v1/management/content/?type=<key>&type=<key>&q=<text>&limit=&offset=` (VIEW permission; at least one `type` is required, otherwise 400; limit max 50)
  - `POST /api/v1/management/markdown/preview/` `{"source": str}` → `{"html": str}` (VIEW permission; enforces the 200_000 char limit; uses `render_markdown_preview`)
  - Editor contract JSON for scalar fields adds `"reference_types": [...]` for REFERENCE, and the `"markdown"`, `"reference"`, `"blob"` input kinds

- [ ] **Step 1: Write failing tests** `test_picker_finds_draft_by_title`, `test_picker_excludes_archived`, `test_picker_requires_type`, `test_picker_requires_view_permission`, `test_preview_escapes_html` (`"<script>x</script>"` → no `<script` in html), `test_editor_contract_exposes_reference_types`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; `make check` → PASS.
- [ ] **Step 5: Commit** `git commit -m "Add management picker search, Markdown preview and editor contract kinds"`

---

### Task 11: Admin editor widgets

**Files:**
- Modify: `src/strata_cms/admin/static/strata/editor.js`, `src/strata_cms/admin/static/strata/editor.css`, `src/strata_cms/admin/templates/admin/strata/content_editor.html`
- Test: `tests/api/test_admin_editor.py`

**Interfaces:**
- Consumes: Task 10 endpoints, Task 6 upload endpoint, Task 1 `publish_readiness`
- Produces (no new JS dependencies; vanilla JS, matching the existing `editor.js`):
  - MARKDOWN: a textarea plus a "Preview" toggle that calls the preview endpoint and inserts the returned HTML into a preview pane
  - REFERENCE: a search input that calls the picker endpoint (debounced 250 ms) and shows a result list with title and status. The selection stores `{"$ref": id}` in the working document; a clear button removes it. Many-reference fields get add/remove/reorder.
  - BLOB: a file input that uploads to the blob endpoint and stores `{"$blob": blob_id}`, with a thumbnail from a temporary object URL. Picker fields whose `reference_types` include `strata.media` also show an "Upload new" action: upload blob → create a `strata.media` draft through the existing create-content endpoint → select it.
  - A readiness banner listing `publish_readiness.problems`; the Publish button is disabled while not ready. Server enforcement remains authoritative.
  - All DOM insertion of user/remote text uses `textContent`. Only the preview HTML from the server preview endpoint uses `innerHTML`.

- [ ] **Step 1: Write failing tests** (server-side rendering/contract only): `test_editor_page_includes_markdown_preview_url`, `test_editor_page_includes_picker_and_upload_urls`, `test_editor_page_shows_readiness_banner_container`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Manual check** with `make run`: create a media item by upload, reference it from the example landing page (add a reference field to the example in `tests` settings only if needed), publish, then fetch it from Delivery. Note the result in the commit body.
- [ ] **Step 5:** `make check` → PASS. Commit `git commit -m "Add admin widgets for Markdown, references, blobs and readiness"`

---

### Task 12: ADRs and documentation

**Files:**
- Create: `docs/adr/0007-publish-readiness.md`, `docs/adr/0008-markdown-rich-text.md`, `docs/adr/0009-media-and-typed-references.md`
- Modify: `docs/adr/0002-content-revision-aggregate.md` and `0003-…` (add "Amended by 0007" notes), `docs/adr/0005-schema-driven-management-editor.md` (link the new input kinds as the deliberate extension), `docs/plugins.md` (sections on Markdown fields, reference/blob fields with `LinkPath`, readiness, `summary_title`, `embed_in_references`), `docs/ai/security.md` (V12 row now "Met: allowlisted, sniffed, size and pixel limited, metadata stripped, server-named"; A10 still N/A), `docs/ai/architecture.md` (projections list), `README.md` (media settings, cleanup and rebuild commands, Next.js `images.remotePatterns` note)

- [ ] **Step 1: Write the ADRs** in the existing ADR format (Status Accepted, Date 2026-11-16, Context/Decision/Consequences/Rejected alternatives), including the three plan deviations and the 60 s cache staleness note.
- [ ] **Step 2:** `make check` → PASS.
- [ ] **Step 3: Commit** `git commit -m "Document readiness, Markdown, media and reference ADRs"`
