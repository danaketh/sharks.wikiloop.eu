"""Rebuild link projections: `python manage.py strata_rebuild_projections`.

Draft scope (titles, draft references, blob usage) is rebuilt from every
revision; the published scope (published references) from published
revisions. Each scope is truncated and refilled in one transaction. Links to
content or blobs that no longer exist are skipped and listed, not fabricated.
The rebuild does not block concurrent writes, so writes committed while it
runs can leave stale projection rows; run it in a maintenance window.
"""

from argparse import ArgumentParser

from django.core.management.base import BaseCommand

from strata_cms.application.content.projection_rebuild import (
    RebuildScope,
    rebuild_projections,
)
from strata_cms.application.ports.content_types import ContentTypeService
from strata_cms.infrastructure.content_types.registry import (
    RegistryContentTypeService,
)
from strata_cms.infrastructure.persistence.django.rebuild import (
    DjangoProjectionRebuildUnitOfWork,
)
from strata_cms.infrastructure.plugins.runtime import get_strata_registry


def _content_type_service() -> ContentTypeService:
    return RegistryContentTypeService(get_strata_registry())


class Command(BaseCommand):
    """Truncate and refill the requested link projection scope(s)."""

    help = (
        "Rebuild titles, reference and blob-usage projections from revisions. "
        "Does not block concurrent writes, so writes committed while it runs "
        "can leave stale projection rows; run it in a maintenance window."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the scope selector."""
        parser.add_argument(
            "--scope",
            choices=[scope.value for scope in RebuildScope],
            default=RebuildScope.ALL.value,
        )

    def handle(self, *args: object, **options: object) -> None:
        """Run the rebuild and print counts plus every skipped item."""
        del args
        scope = RebuildScope(str(options["scope"]))
        report = rebuild_projections(
            scope,
            new_uow=DjangoProjectionRebuildUnitOfWork,
            content_types=_content_type_service(),
        )
        for link in report.skipped_links:
            self.stderr.write(
                f"skipped {link.scope} {link.kind} link {link.location} of "
                f"content {link.content_id} revision {link.revision_id}: "
                f"target {link.target} does not exist"
            )
        for revision in report.skipped_revisions:
            self.stderr.write(
                f"skipped {revision.scope} revision {revision.revision_id} of "
                f"content {revision.content_id}: {revision.reason}"
            )
        self.stdout.write(
            f"rebuilt scope={scope.value}: "
            f"{report.draft_revisions} draft revision(s), "
            f"{report.published_revisions} published revision(s), "
            f"{len(report.skipped_links)} skipped link(s), "
            f"{len(report.skipped_revisions)} skipped revision(s)"
        )
