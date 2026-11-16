"""Django Storage API adapter for media bytes."""

from django.core.files.base import ContentFile
from django.core.files.storage import Storage, default_storage


class StorageKeyConflictError(RuntimeError):
    """Raised when storage could not store bytes at the exact requested key."""


class DjangoMediaFileStore:
    """`MediaFileStore` over a Django storage (``default_storage`` by default)."""

    def __init__(self, storage: Storage | None = None) -> None:
        """Bind the adapter to a Django storage backend."""
        self._storage = storage if storage is not None else default_storage

    def save(self, key: str, data: bytes) -> None:
        """Store ``data`` at exactly ``key``.

        Django storages silently rename on collision; a renamed file would
        desynchronize the recorded key, so it is removed and an error raised.
        """
        stored_name = self._storage.save(key, ContentFile(data))
        if stored_name != key:
            self._storage.delete(stored_name)
            raise StorageKeyConflictError(key)

    def url(self, key: str) -> str:
        """Return the storage backend's URL for ``key``."""
        return self._storage.url(key)

    def delete(self, key: str) -> None:
        """Delete ``key``; Django storages treat a missing file as a no-op."""
        self._storage.delete(key)
