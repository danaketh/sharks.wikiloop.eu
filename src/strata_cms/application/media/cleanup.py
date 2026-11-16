"""Delete media blobs that no revision uses once a grace period has passed.

A blob counts as used while any revision of any content (historical revisions
included) links to it, per the blob usage projection. The grace period keeps
fresh uploads alive until the revision referencing them is saved.
"""

from datetime import timedelta

from strata_cms.application.ports.clock import Clock
from strata_cms.application.ports.media import MediaBlobRepository, MediaFileStore


def cleanup_orphan_blobs(
    *,
    blobs: MediaBlobRepository,
    files: MediaFileStore,
    clock: Clock,
    grace: timedelta,
    batch_size: int = 100,
) -> int:
    """Delete unused blobs created before ``now - grace``; return the count.

    Each record is deleted first, and only if it is still unused at that
    moment; its file is deleted only after that. A missing file is not an
    error, so an interrupted run can simply be repeated. A file whose record
    was deleted but whose deletion then failed stays behind as untracked bytes.
    """
    if grace < timedelta(0):
        raise ValueError("Orphan grace period must not be negative.")
    if batch_size < 1:
        raise ValueError("Orphan cleanup batch size must be positive.")
    cutoff = clock.now() - grace
    deleted = 0
    while True:
        candidates = blobs.list_orphans(created_before=cutoff, limit=batch_size)
        batch_deleted = 0
        for blob in candidates:
            if blobs.delete_if_unused(blob.id):
                files.delete(blob.storage_key)
                batch_deleted += 1
        deleted += batch_deleted
        # Kept candidates gained a usage row and drop out of the next listing;
        # stopping on an unproductive batch also guards against a repository
        # that keeps listing rows it refuses to delete.
        if len(candidates) < batch_size or batch_deleted == 0:
            return deleted
