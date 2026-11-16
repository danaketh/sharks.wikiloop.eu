"""Input serializers for management write commands."""

from typing import Any

from rest_framework import serializers

from strata_cms.application.errors import MEDIA_REJECTION_REASONS


class CreateContentSerializer(serializers.Serializer[Any]):
    """Validate creation of a content identity and first revision."""

    type = serializers.CharField(max_length=200)
    schema_version = serializers.IntegerField(min_value=1)
    # Field name shadows BaseSerializer.data in the stubs; read through
    # validated_data (never the .data property), so this is safe at runtime.
    data = serializers.DictField()  # type: ignore[assignment]


class CreateRevisionSerializer(serializers.Serializer[Any]):
    """Validate appending a revision with optimistic concurrency."""

    expected_version = serializers.IntegerField(min_value=0)
    schema_version = serializers.IntegerField(min_value=1)
    data = serializers.DictField()  # type: ignore[assignment]


class PublishRevisionSerializer(serializers.Serializer[Any]):
    """Validate publishing a selected immutable revision."""

    expected_version = serializers.IntegerField(min_value=0)
    revision_id = serializers.UUIDField()


class ContentLifecycleSerializer(serializers.Serializer[Any]):
    """Validate an archive/restore command using optimistic concurrency."""

    expected_version = serializers.IntegerField(min_value=0)


class AttachRouteSerializer(serializers.Serializer[Any]):
    """Validate attaching a new route to content that has none yet."""

    parent_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    slug = serializers.CharField(max_length=200)


class MoveRouteSerializer(serializers.Serializer[Any]):
    """Validate moving/renaming an existing route."""

    parent_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    slug = serializers.CharField(max_length=200)
    expected_version = serializers.IntegerField(min_value=0)


class MediaBlobUploadSerializer(serializers.Serializer[Any]):
    """Validate a multipart media upload; the file name is never used."""

    file = serializers.FileField(use_url=False)
    preserve_original = serializers.BooleanField(default=False)


class MediaBlobSerializer(serializers.Serializer[Any]):
    """Describe a stored media blob (documentation of the 201 response)."""

    blob_id = serializers.UUIDField()
    content_type = serializers.CharField()
    size_bytes = serializers.IntegerField(min_value=0)
    width = serializers.IntegerField(min_value=1)
    height = serializers.IntegerField(min_value=1)
    sha256 = serializers.CharField(min_length=64, max_length=64)


class MediaProblemSerializer(serializers.Serializer[Any]):
    """Describe a rejected media upload (documentation of the 400 response)."""

    detail = serializers.CharField()
    code = serializers.ChoiceField(choices=sorted(MEDIA_REJECTION_REASONS))


class ErrorDetailSerializer(serializers.Serializer[Any]):
    """Describe a plain ``{"detail": ...}`` error response."""

    detail = serializers.CharField()
