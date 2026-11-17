# ADR 0008: Markdown is the rich-text format

- Status: Accepted
- Date: 2026-11-16

## Context

Species articles will be imported from Wikipedia and edited locally, and a later
project will diff and three-way merge article text. The stored format must be
line-diffable, safe to render without a frontend HTML sanitizer, and validated
server-side.

## Decision

Rich text is restricted **CommonMark Markdown** stored as source text.

- Contract in `strata_cms.plugin_api`: `MarkdownText` (frozen dataclass wrapping
  the source), `MarkdownProfile`, `DEFAULT_MARKDOWN_PROFILE`, and the pure
  functions `validate_markdown`, `normalize_markdown` and
  `render_markdown_preview`.
- Parsing uses `markdown-it-py` (now a direct runtime dependency) with raw HTML
  disabled. The parser is used only to validate, preview, and walk tokens; the
  **stored value is the source Markdown**.
- Default profile: paragraphs, h2-h4, emphasis/strong, inline code, code
  blocks, links, ordered/unordered lists, blockquotes, GFM tables, horizontal
  rules. Rejected: raw HTML (block and inline), images, h1, h5-h6, footnotes.
  Images are modeled as media references and the page title is a field, not an
  h1.
- Links: allowed schemes are `https`, `http`, `mailto`, and relative paths.
  Mixed-case, entity-encoded and whitespace-obfuscated dangerous schemes
  (`JaVaScRiPt:`, `&#106;avascript:`, `java<TAB>script:`) are rejected.
- Limits: 200,000 characters per field and a nesting-depth cap.
- Normalization on every write: line endings to `\n`, trailing whitespace
  stripped per line (hard line breaks preserved), single trailing newline.
  Markdown is never reflowed or re-rendered, so unchanged text produces
  unchanged diffs.
- Disallowed constructs are rejected as write validation issues, never silently
  sanitized, so editors see and fix the problem.
- Editor: `EditorInputKind.MARKDOWN`. The Management API offers
  `POST /api/v1/manage/markdown/preview/`, which renders with HTML disabled;
  the Unfold admin shows a textarea with live preview and no CDN scripts.
- Delivery emits the Markdown string unchanged. The Next.js frontend renders it
  with `react-markdown` and no raw-HTML plugin.
- A core `strata.rich_text` block (plugin `strata_cms.contrib.rich_text`) holds
  one Markdown field so block-based content types can use rich text at once.

## Consequences

- Text diffs and merges as plain lines.
- No server or client HTML sanitizer is needed for rich text.
- Wikipedia content that uses tables is representable; footnotes and inline
  images are not and must be converted or dropped at import time.
- Search keeps indexing the raw Markdown string. The existing text extraction
  flattens strings and PostgreSQL tokenization ignores Markdown punctuation, so
  no Markdown-aware extractor was added. Revisit if relevance suffers.
- A plugin that needs a stricter or looser subset can pass its own
  `MarkdownProfile` to `validate_markdown`.

## Rejected alternatives

### ProseMirror/TipTap JSON

Rejected: renders well but diffs and merges poorly, which would block the text
sync project.

### Sanitized HTML

Rejected: needs a sanitizer on every render path and produces noisier diffs.

### Silently sanitizing or re-rendering Markdown on save

Rejected: destroys the author's source, makes diffs unstable and hides problems
from the editor.
