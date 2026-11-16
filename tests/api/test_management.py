from collections.abc import Mapping

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse
from rest_framework.test import APIClient

from strata_cms.api.management import views
from strata_cms.application.errors import ContentDataProblem
from strata_cms.application.ports.content_types import (
    ContentTypeService,
    PreparedContentData,
)
from strata_cms.config.services import get_content_type_service
from strata_cms.domain.revision import Revision
from strata_cms.domain.value_objects import ContentTypeKey
from strata_cms.examples.simple_article import EXAMPLE_CONTENT_TYPE
from strata_cms.infrastructure.persistence.django.models import ContentRecord

pytestmark = pytest.mark.django_db


def _staff_client() -> APIClient:
    user_model = get_user_model()
    user = user_model.objects.create_superuser(
        username="editor",
        email="editor@example.invalid",
        password="test-password",
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _staff_client_with_perms(
    *codenames: str, username: str = "scoped-staff"
) -> APIClient:
    user_model = get_user_model()
    user = user_model.objects.create_user(
        username=username,
        password="test-password",
        is_staff=True,
    )
    permissions = Permission.objects.filter(
        content_type__app_label="strata_persistence",
        codename__in=codenames,
    )
    user.user_permissions.set(permissions)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_management_editor_schema_lists_installed_examples() -> None:
    client = _staff_client()

    response = client.get(reverse("api:management:content-types"))

    assert response.status_code == 200
    keys = {item["key"] for item in response.json()["results"]}
    assert "strata_examples.article" in keys
    assert "strata_examples.landing_page" in keys


def test_management_content_revision_and_publish_flow() -> None:
    client = _staff_client()
    create = client.post(
        reverse("api:management:content-create"),
        {
            "type": str(EXAMPLE_CONTENT_TYPE),
            "schema_version": 2,
            "data": {"title": "First", "body": "Body"},
        },
        format="json",
    )
    assert create.status_code == 201
    created = create.json()
    assert created["content_version"] == 1
    content_id = created["content_id"]

    detail = client.get(reverse("api:management:content-detail", args=[content_id]))
    assert detail.status_code == 200
    assert detail.json()["data"]["title"] == "First"

    revision = client.post(
        reverse("api:management:content-revision-create", args=[content_id]),
        {
            "expected_version": 1,
            "schema_version": 2,
            "data": {"title": "Second", "body": "Changed"},
        },
        format="json",
    )
    assert revision.status_code == 201
    revised = revision.json()
    assert revised["content_version"] == 2

    stale = client.post(
        reverse("api:management:content-revision-create", args=[content_id]),
        {
            "expected_version": 1,
            "schema_version": 2,
            "data": {"title": "Stale", "body": "Lost update"},
        },
        format="json",
    )
    assert stale.status_code == 409

    publish = client.post(
        reverse("api:management:content-publish", args=[content_id]),
        {
            "expected_version": 2,
            "revision_id": revised["revision_id"],
        },
        format="json",
    )
    assert publish.status_code == 200
    assert publish.json()["content_version"] == 3
    assert ContentRecord.objects.get(pk=content_id).published_revision_id is not None


def test_management_revision_history_and_detail() -> None:
    client = _staff_client()
    create = client.post(
        reverse("api:management:content-create"),
        {
            "type": str(EXAMPLE_CONTENT_TYPE),
            "schema_version": 2,
            "data": {"title": "First", "body": "Body"},
        },
        format="json",
    )
    created = create.json()
    content_id = created["content_id"]

    revision = client.post(
        reverse("api:management:content-revision-create", args=[content_id]),
        {
            "expected_version": 1,
            "schema_version": 2,
            "data": {"title": "Second", "body": "Changed"},
        },
        format="json",
    )
    revised = revision.json()

    history = client.get(
        reverse("api:management:content-revision-create", args=[content_id])
    )
    assert history.status_code == 200
    results = history.json()["results"]
    assert [entry["number"] for entry in results] == [2, 1]
    assert all(not entry["is_published"] for entry in results)

    publish = client.post(
        reverse("api:management:content-publish", args=[content_id]),
        {
            "expected_version": 2,
            "revision_id": created["revision_id"],
        },
        format="json",
    )
    assert publish.status_code == 200

    history_after_publish = client.get(
        reverse("api:management:content-revision-create", args=[content_id])
    ).json()["results"]
    published_flags = {
        entry["revision_id"]: entry["is_published"] for entry in history_after_publish
    }
    assert published_flags[created["revision_id"]] is True
    assert published_flags[revised["revision_id"]] is False

    detail = client.get(
        reverse(
            "api:management:content-revision-detail",
            args=[content_id, created["revision_id"]],
        )
    )
    assert detail.status_code == 200
    assert detail.json()["data"]["title"] == "First"

    missing = client.get(
        reverse(
            "api:management:content-revision-detail",
            args=[content_id, "00000000-0000-0000-0000-000000000000"],
        )
    )
    assert missing.status_code == 404


def test_management_archive_blocks_edits_and_restore_reallows_them() -> None:
    client = _staff_client()
    create = client.post(
        reverse("api:management:content-create"),
        {
            "type": str(EXAMPLE_CONTENT_TYPE),
            "schema_version": 2,
            "data": {"title": "First", "body": "Body"},
        },
        format="json",
    )
    created = create.json()
    content_id = created["content_id"]

    archive = client.post(
        reverse("api:management:content-archive", args=[content_id]),
        {"expected_version": created["content_version"]},
        format="json",
    )
    assert archive.status_code == 200
    archived = archive.json()
    assert archived["is_archived"] is True

    blocked_revision = client.post(
        reverse("api:management:content-revision-create", args=[content_id]),
        {
            "expected_version": archived["content_version"],
            "schema_version": 2,
            "data": {"title": "Blocked", "body": "Body"},
        },
        format="json",
    )
    assert blocked_revision.status_code == 409

    blocked_publish = client.post(
        reverse("api:management:content-publish", args=[content_id]),
        {
            "expected_version": archived["content_version"],
            "revision_id": created["revision_id"],
        },
        format="json",
    )
    assert blocked_publish.status_code == 409

    detail = client.get(reverse("api:management:content-detail", args=[content_id]))
    assert detail.json()["is_archived"] is True

    restore = client.post(
        reverse("api:management:content-restore", args=[content_id]),
        {"expected_version": archived["content_version"]},
        format="json",
    )
    assert restore.status_code == 200
    restored = restore.json()
    assert restored["is_archived"] is False

    allowed_revision = client.post(
        reverse("api:management:content-revision-create", args=[content_id]),
        {
            "expected_version": restored["content_version"],
            "schema_version": 2,
            "data": {"title": "Allowed", "body": "Body"},
        },
        format="json",
    )
    assert allowed_revision.status_code == 201


def test_management_archive_rejects_stale_version() -> None:
    client = _staff_client()
    create = client.post(
        reverse("api:management:content-create"),
        {
            "type": str(EXAMPLE_CONTENT_TYPE),
            "schema_version": 2,
            "data": {"title": "First", "body": "Body"},
        },
        format="json",
    )
    content_id = create.json()["content_id"]

    stale = client.post(
        reverse("api:management:content-archive", args=[content_id]),
        {"expected_version": 0},
        format="json",
    )

    assert stale.status_code == 409


def test_publish_requires_separate_permission_from_change() -> None:
    admin = _staff_client()
    create = admin.post(
        reverse("api:management:content-create"),
        {
            "type": str(EXAMPLE_CONTENT_TYPE),
            "schema_version": 2,
            "data": {"title": "First", "body": "Body"},
        },
        format="json",
    )
    created = create.json()
    content_id = created["content_id"]

    editor = _staff_client_with_perms("view_contentrecord", "change_contentrecord")

    revision = editor.post(
        reverse("api:management:content-revision-create", args=[content_id]),
        {
            "expected_version": created["content_version"],
            "schema_version": 2,
            "data": {"title": "Edited by scoped editor", "body": "Body"},
        },
        format="json",
    )
    assert revision.status_code == 201

    denied_publish = editor.post(
        reverse("api:management:content-publish", args=[content_id]),
        {
            "expected_version": revision.json()["content_version"],
            "revision_id": revision.json()["revision_id"],
        },
        format="json",
    )
    assert denied_publish.status_code == 403

    publisher = _staff_client_with_perms(
        "view_contentrecord",
        "change_contentrecord",
        "publish_contentrecord",
        username="publisher",
    )
    allowed_publish = publisher.post(
        reverse("api:management:content-publish", args=[content_id]),
        {
            "expected_version": revision.json()["content_version"],
            "revision_id": revision.json()["revision_id"],
        },
        format="json",
    )
    assert allowed_publish.status_code == 200


def test_management_api_rejects_non_staff_users() -> None:
    client = APIClient()

    response = client.get(reverse("api:management:content-types"))

    assert response.status_code in {401, 403}


def test_management_api_respects_django_permissions_for_staff() -> None:
    user_model = get_user_model()
    user = user_model.objects.create_user(
        username="staff-no-perms",
        password="test-password",
        is_staff=True,
    )
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.get(reverse("api:management:content-types"))

    assert response.status_code == 403


class _NotReadyContentTypes:
    """Real content-type service plus a fixed publish-readiness problem."""

    def __init__(self, inner: ContentTypeService) -> None:
        self._inner = inner

    def prepare_for_write(
        self,
        *,
        type_key: ContentTypeKey,
        schema_version: int,
        data: Mapping[str, object],
    ) -> PreparedContentData:
        return self._inner.prepare_for_write(
            type_key=type_key, schema_version=schema_version, data=data
        )

    def validate_revision(self, revision: Revision) -> None:
        self._inner.validate_revision(revision)

    def publish_issues(self, revision: Revision) -> tuple[ContentDataProblem, ...]:
        del revision
        return (
            ContentDataProblem(
                code="fake_not_ready",
                message="Not ready.",
                path="$.body",
            ),
        )

    def delivery_data(self, revision: Revision) -> dict[str, object]:
        return self._inner.delivery_data(revision)


def _create_article(client: APIClient) -> dict[str, object]:
    create = client.post(
        reverse("api:management:content-create"),
        {
            "type": str(EXAMPLE_CONTENT_TYPE),
            "schema_version": 2,
            "data": {"title": "First", "body": "Body"},
        },
        format="json",
    )
    assert create.status_code == 201
    created: dict[str, object] = create.json()
    return created


def test_publish_not_ready_returns_422_with_problems(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _staff_client()
    created = _create_article(client)
    inner = get_content_type_service()
    monkeypatch.setattr(
        views, "get_content_type_service", lambda: _NotReadyContentTypes(inner)
    )

    response = client.post(
        reverse("api:management:content-publish", args=[created["content_id"]]),
        {"expected_version": 1, "revision_id": created["revision_id"]},
        format="json",
    )

    assert response.status_code == 422
    body = response.json()
    assert body["problems"] == [
        {"code": "fake_not_ready", "message": "Not ready.", "path": "$.body"}
    ]
    assert (
        ContentRecord.objects.get(pk=str(created["content_id"])).published_revision_id
        is None
    )


def test_document_includes_publish_readiness_for_latest_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _staff_client()
    created = _create_article(client)
    url = reverse("api:management:content-detail", args=[created["content_id"]])

    ready = client.get(url).json()
    assert ready["publish_readiness"] == {"ready": True, "problems": []}

    inner = get_content_type_service()
    monkeypatch.setattr(
        views, "get_content_type_service", lambda: _NotReadyContentTypes(inner)
    )
    not_ready = client.get(url).json()

    assert not_ready["publish_readiness"]["ready"] is False
    assert not_ready["publish_readiness"]["problems"][0]["code"] == "fake_not_ready"
