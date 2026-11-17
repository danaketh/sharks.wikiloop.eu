# ADR 0007: Publish readiness is separate from write validation

- Status: Accepted
- Date: 2026-11-16
- Amends: ADR 0002, ADR 0003

## Context

ADR 0002/0003 made schema `validate` the single gate for every revision write.
That forces every required field to be filled before a draft can be saved,
which breaks legitimate workflows: an upload should create a media draft before
anyone has written alt text, and an importer should be able to save a partial
article. At the same time, Delivery must never expose an incomplete item.

## Decision

Two validation levels, both pure (no database, network, or storage access):

- **Write validation** (`ContentSchema.validate` / `BlockSchema.validate`):
  shape, types, and safety (for example the Markdown subset, https-only URLs).
  Runs on every create/revision write, unchanged.
- **Publish readiness**: completeness rules. A `ContentTypeDefinition` or
  `BlockDefinition` supplies an optional `readiness` object
  (`PublishReadiness.publish_issues(value)`); the default is no issues.

`publish_content` runs write validation and publish readiness on the revision
being published, recursing into blocks, and rejects with all issues if any.
`create_content`/`create_revision` never run readiness. Readiness is evaluated
after in-memory migration to the current schema, so historical revisions are
judged against current rules without rewriting history.

The Management API exposes `publish_readiness: {ready, problems[]}` on the
latest working revision so editors can see "not ready to publish" without
attempting a publish. Problems carry stable codes: `strata.media` reports
`alt_text_required` and `attribution_incomplete` as readiness problems, while
its write-validation codes include `file_required` and
`attribution_url_not_https`.

Link cardinality follows the same split: reference `min_items` is a readiness
rule, not a write rule, so partially filled drafts still save.

## Consequences

- Imports, uploads and half-written drafts can be saved and revised.
- A published revision is complete under the schema in force at publish time. A
  later plugin upgrade that tightens readiness does not unpublish existing
  content; the next publish applies the new rules.
- Plugin authors put "required to be useful" rules in `readiness`, and only
  "must never be persisted" rules in `validate`.
- ADR 0002's publish use case and ADR 0003's schema contract gain this second
  hook; nothing else in either ADR changes.

## Rejected alternatives

### Required fields enforced on every write

Rejected: blocks upload-then-describe flows and partial imports (the planned
sync projects produce drafts that are incomplete by nature).

### Readiness stored as a revision status column

Rejected: it would be derived state stored beside the data it derives from and
would go stale when schemas change. Computing it from the revision keeps it
correct by construction.

### Readiness checked in the admin client only

Rejected: the API and CLI would bypass it. It lives in the shared application
use case, per the rule that business use cases exist once.
