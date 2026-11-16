"""Delete unused media blobs: `python manage.py strata_cleanup_media`.

A blob is deleted when no revision of any content links to it (per the blob
usage projection) and it is older than ``STRATA_MEDIA_ORPHAN_GRACE``. The
record is deleted first, then its file; rerunning after a failure is safe.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from strata_cms.application.media.cleanup import cleanup_orphan_blobs
from strata_cms.infrastructure.clock.system import DjangoClock
from strata_cms.infrastructure.media.storage import DjangoMediaFileStore
from strata_cms.infrastructure.persistence.django.media import (
    DjangoMediaBlobRepository,
)


class Command(BaseCommand):
    """Delete orphan media blobs past the configured grace period."""

    help = __doc__

    def handle(self, *args: object, **options: object) -> None:
        """Run the cleanup and print the number of deleted blobs."""
        del args, options
        grace = settings.STRATA_MEDIA_ORPHAN_GRACE
        if not isinstance(grace, timedelta):
            raise CommandError("STRATA_MEDIA_ORPHAN_GRACE must be a timedelta.")
        deleted = cleanup_orphan_blobs(
            blobs=DjangoMediaBlobRepository(),
            files=DjangoMediaFileStore(),
            clock=DjangoClock(),
            grace=grace,
        )
        self.stdout.write(f"deleted {deleted} orphan media blob(s)")
