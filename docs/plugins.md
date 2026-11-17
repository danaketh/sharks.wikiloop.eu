# Writing a Strata plugin

The normative architecture is ADR 0003 and `docs/ai/architecture.md`. This page
shows the intended developer workflow.

## Principles

A plugin is a normal Python/Django package with stable CMS metadata. It declares
what it contributes; registry compilation must not perform application I/O.

Plugins should import stable CMS contracts from:

```python
from strata_cms.plugin_api import ...
from strata_cms.plugin_api.django import StrataPluginConfig
```

Do not import `strata_cms.infrastructure.*` or other private implementation
modules from a third-party plugin.

## Define plugin metadata

Use a stable namespaced key. The package version, supported CMS version and
plugin API version are independent from persisted content schema versions.

```python
from strata_cms.plugin_api import PluginDefinition, PluginKey

PLUGIN = PluginDefinition(
    key=PluginKey("acme_blog.core"),
    version="1.2.0",
    requires_strata=">=0.1,<0.2",
)
```

Dependencies on other Strata plugins are explicit:

```python
from strata_cms.plugin_api import PluginRequirement

requires_plugins = (PluginRequirement(PluginKey("acme_media.core"), ">=1,<2"),)
```

Registry compilation rejects missing/incompatible dependencies and cycles.

## Define typed content data

The Django ORM is not the content schema. Revision JSON maps to a plain typed
value:

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArticleData:
    title: str
    body: str
```

Implement `ContentSchema[T]` to deserialize current JSON, serialize typed values
and return semantic validation issues. Schema methods do not query the database
or external services.

```python
from strata_cms.domain.revision import RevisionData
from strata_cms.plugin_api import ContentSchema, ValidationIssue


class ArticleSchema(ContentSchema[ArticleData]):
    value_type = ArticleData

    def deserialize(self, data: RevisionData) -> ArticleData:
        raw = data.as_dict()
        # Validate shape/types explicitly here.
        ...

    def serialize(self, value: ArticleData) -> RevisionData:
        return RevisionData.from_mapping({"title": value.title, "body": value.body})

    def validate(self, value: ArticleData) -> tuple[ValidationIssue, ...]: ...
```

## Evolve persisted schemas

Increment the schema version when persisted revision shape changes. Supply every
adjacent migration needed from version 1 to the current version:

```python
from strata_cms.plugin_api import SchemaMigration, SchemaMigrationSet

MIGRATIONS = SchemaMigrationSet(
    (
        SchemaMigration(1, 2, migrate_v1_to_v2),
        SchemaMigration(2, 3, migrate_v2_to_v3),
    )
)
```

Migration functions take and return `RevisionData`. They are pure: no ORM,
network, cache, broker or filesystem work.

Historical revision rows are not rewritten during normal plugin upgrades. They
are migrated in memory when read. New writes are decoded/validated and persisted
using the current canonical schema version.

## Register a content type

```python
from strata_cms.domain.value_objects import ContentTypeKey
from strata_cms.plugin_api import ContentCapability, ContentTypeDefinition

ARTICLE = ContentTypeDefinition(
    key=ContentTypeKey("acme_blog.article"),
    schema_version=3,
    schema=ArticleSchema(),
    migrations=MIGRATIONS,
    capabilities=frozenset(
        {
            ContentCapability.PUBLISHABLE,
            ContentCapability.PREVIEWABLE,
        }
    ),
)
```

The optional Delivery serializer can expose a public API representation that is
different from the canonical stored schema.

## Django registration

A Django plugin app subclasses `StrataPluginConfig`:

```python
from strata_cms.plugin_api import PluginRegistrar
from strata_cms.plugin_api.django import StrataPluginConfig


class BlogPluginConfig(StrataPluginConfig):
    name = "acme_blog"
    strata_plugin = PLUGIN

    def register_strata(self, registrar: PluginRegistrar) -> None:
        registrar.register_content_type(ARTICLE)
```

Add the AppConfig to `INSTALLED_APPS`. Strata centrally collects installed
`StrataPluginConfig` applications after Django app loading. Each callback receives
an owner-scoped `PluginRegistrar`, not the raw registry builder.

Do **not** register by mutating a global object from `AppConfig.ready()`.

## Validate the plugin

At minimum run:

```bash
make architecture-check
make check
```

Tests for a schema change should cover:

- current-version decode/encode;
- semantic validation failures;
- every supported historical migration path;
- Delivery serialization after historical migration;
- plugin dependency/version failures when relevant.

The non-installed `strata_cms.examples.simple_article` package is a complete
small reference implementation used by the repository tests.

## Define structured blocks

Blocks are immutable values embedded inside revision JSON. The CMS owns the
stable envelope (`id`, `type`, `version`, `data`, `slots`); a plugin schema owns
only the typed `data` object.

```python
from dataclasses import dataclass

from strata_cms.domain.revision import RevisionData
from strata_cms.plugin_api import (
    BlockDefinition,
    BlockSchema,
    BlockTypeKey,
    ValidationIssue,
)


@dataclass(frozen=True, slots=True)
class QuoteData:
    text: str
    attribution: str


class QuoteSchema(BlockSchema[QuoteData]):
    value_type = QuoteData

    def deserialize(self, data: RevisionData) -> QuoteData: ...

    def serialize(self, value: QuoteData) -> RevisionData: ...

    def validate(self, value: QuoteData) -> tuple[ValidationIssue, ...]: ...


QUOTE = BlockDefinition(
    key=BlockTypeKey("acme_blog.quote"),
    schema_version=1,
    schema=QuoteSchema(),
)
```

Block schema versions evolve independently from the surrounding content type.
Use `SchemaMigrationSet` exactly as for content types. Migrations are pure and
must be deterministic because the same historical revision can be decoded many
times.

### Container blocks and named slots

Nested blocks use CMS-owned named slots rather than storing arbitrary child
arrays inside block `data`:

```python
from strata_cms.plugin_api import BlockListConstraint, BlockSlotDefinition

SECTION = BlockDefinition(
    key=BlockTypeKey("acme_blog.section"),
    schema_version=1,
    schema=SectionSchema(),
    slots=(
        BlockSlotDefinition(
            name="children",
            constraint=BlockListConstraint(
                allowed_types=frozenset({QUOTE.key}),
                min_items=1,
            ),
        ),
    ),
)
```

A two-column layout can instead declare `left` and `right`. Slot names,
cardinality, allowed block types, unknown slots and maximum nesting depth are
validated generically by the registry.

If a slot or content field explicitly references a block owned by another
plugin, declare that plugin in `requires_plugins`; registry compilation rejects
undeclared cross-plugin references.

### Declare block fields on content

A content type tells Strata where block collections live in its revision
JSON:

```python
from strata_cms.plugin_api import BlockFieldDefinition, BlockListConstraint

PAGE = ContentTypeDefinition(
    key=ContentTypeKey("acme_pages.page"),
    schema_version=1,
    schema=PageSchema(),
    block_fields=(
        BlockFieldDefinition(
            path=("body",),
            constraint=BlockListConstraint(
                allowed_types=frozenset({QUOTE.key, SECTION.key}),
            ),
        ),
    ),
)
```

The typed content value may use `BlockCollection` for that field. On every new
revision write, Strata recursively migrates and validates the complete block
tree, preserves block UUIDs, and stores current canonical block schema versions.
Historical revisions remain unchanged.

The optional `delivery_path` on `BlockFieldDefinition` can point to a different
object path when a content Delivery serializer renames the field. Strata
then recursively applies each block's Delivery serializer at that path.

Register blocks through the same owner-scoped registrar:

```python
def register_strata(self, registrar: PluginRegistrar) -> None:
    registrar.register_block_type(QUOTE)
    registrar.register_block_type(SECTION)
    registrar.register_content_type(PAGE)
```

See `strata_cms.examples.structured_page` for a tested example containing a
migrated text block, a nested section slot, a block-backed content type and
recursive Delivery serialization.

## Markdown fields

Rich text is restricted Markdown (ADR 0008). Hold it as `MarkdownText`, validate
it with `validate_markdown` in write validation, and normalize it on decode and
encode so stored text is canonical:

```python
from strata_cms.plugin_api import MarkdownText, normalize_markdown, validate_markdown

def deserialize(self, data):
    return Article(body=MarkdownText(normalize_markdown(str(data.as_dict()["body"]))))

def validate(self, value):
    return validate_markdown(value.body.source, path="$.body")
```

The default profile rejects raw HTML, images, h1 and h5-h6, and allows only
`https`, `http`, `mailto` and relative links. Pass a `MarkdownProfile` to
`validate_markdown` for a different subset. Mark the editor field with
`EditorInputKind.MARKDOWN`. The core `strata.rich_text` block already wraps one
Markdown field.

## Reference and blob fields

Links are stored as `{"$ref": "<uuid>"}` (another content item) or
`{"$blob": "<uuid>"}` (an uploaded file). Declare each one with a `LinkPath`
(`[*]` means every list item) on the content type or block definition; a link
object at an undeclared path is rejected on write:

```python
from strata_cms.plugin_api import (
    BlobFieldDefinition,
    ContentTypeDefinition,
    LinkPath,
    ReferenceFieldDefinition,
)

SPECIES = ContentTypeDefinition(
    key=ContentTypeKey("acme_sharks.species"),
    schema_version=1,
    schema=SpeciesSchema(),
    reference_fields=(
        ReferenceFieldDefinition(
            path=LinkPath.parse("photos"),
            allowed_types=frozenset({ContentTypeKey("strata.media")}),
            many=True,
            max_items=10,
        ),
    ),
    blob_fields=(BlobFieldDefinition(LinkPath.parse("cover.file")),),
)
```

Rules:

- A reference to another plugin's type requires a declared plugin dependency
  (for `strata.media`, depend on the `strata.media` plugin), and every allowed
  target type must define `summary_title`.
- On write, targets must exist, not be archived, and have an allowed type;
  they need not be published. Your schema only sees the raw `{"$ref": ...}`
  objects.
- `min_items` is a publish-readiness rule, not a write rule.
- Delivery replaces each `$ref` with a summary `{id, type, title, path}` (or
  `null`/omitted when the target is not published) and each `$blob` with a file
  descriptor. Resolved links are cached with the response for 60 s, so titles and
  paths may be briefly stale.
- Editor fields: `EditorInputKind.REFERENCE` takes `reference_types`;
  `EditorInputKind.BLOB` marks an upload target.

## Publish readiness

Write validation (`validate`) must accept anything that is safe to store.
Completeness rules go in an optional `readiness` object, which `publish_content`
runs and `create_revision` never does (ADR 0007):

```python
from strata_cms.plugin_api import ValidationIssue

class ArticleReadiness:
    def publish_issues(self, value: Article) -> tuple[ValidationIssue, ...]:
        if value.title.strip():
            return ()
        return (ValidationIssue(path="$.title", message="Title is required.", code="title_required"),)

ARTICLE = ContentTypeDefinition(..., readiness=ArticleReadiness())
```

Readiness must be pure. The Management API reports the current problems as
`publish_readiness` on the latest revision.

## Summaries and embedding

- `summary_title=callable(value) -> str` gives a type a display title for
  reference pickers and Delivery summaries. It is required on any type listed
  in a `ReferenceFieldDefinition.allowed_types`.
- `embed_in_references=True` makes Delivery include the target's own Delivery
  payload under `data` in reference summaries, so clients need no second
  request. `strata.media` uses it.

## Describe generic editing

Editor metadata is optional and presentation-neutral. It tells Management
clients how to present current schema data; it does not replace `ContentSchema`
or `BlockSchema` validation.
Fields such as `required` and `read_only` are UX hints only; plugins must not
treat them as authorization or as the only source of validation.

```python
from strata_cms.domain.revision import RevisionData
from strata_cms.plugin_api import (
    ContentEditorDefinition,
    EditorInputKind,
    ScalarEditorField,
)

ARTICLE = ContentTypeDefinition(
    key=ContentTypeKey("acme_blog.article"),
    schema_version=2,
    schema=ArticleSchema(),
    editor=ContentEditorDefinition(
        label="Article",
        initial_data=RevisionData.from_mapping({"title": "", "body": ""}),
        fields=(
            ScalarEditorField(
                path=("title",),
                label="Title",
                input_kind=EditorInputKind.TEXT,
            ),
            ScalarEditorField(
                path=("body",),
                label="Body",
                input_kind=EditorInputKind.TEXTAREA,
            ),
        ),
    ),
)
```

For content block collections, reference an existing semantic
`BlockFieldDefinition` with editor metadata rather than duplicating its allowed
block types/cardinality:

```python
from strata_cms.plugin_api import BlockCollectionEditorField

editor = ContentEditorDefinition(
    label="Page",
    fields=(
        BlockCollectionEditorField(
            path=("body",),
            label="Body",
        ),
    ),
)
```

Blocks may similarly provide `BlockEditorDefinition` with scalar field hints,
slot labels/help text and initial working data. Slot child constraints still
come from the block's `BlockSlotDefinition`.

The built-in Management editor creates/reorders/nests blocks inside an ephemeral
working document. It allocates a stable block UUID when the block is created,
but nothing becomes persistent until the complete document is submitted as a
new revision. Server-side content/block schemas then normalize and validate the
entire tree.

Plugins without editor metadata still work for imports, custom management UIs,
and Delivery; they are simply reported as not generically editable.
