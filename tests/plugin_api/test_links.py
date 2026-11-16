from uuid import UUID

import pytest

from strata_cms.domain.value_objects import ContentTypeKey
from strata_cms.plugin_api.errors import RegistryConfigurationError, SchemaDecodeError
from strata_cms.plugin_api.links import (
    BlobFieldDefinition,
    BlobReference,
    ContentReference,
    LinkPath,
    ReferenceFieldDefinition,
    find_undeclared_links,
    iter_link_values,
)

U1 = UUID("00000000-0000-0000-0000-000000000001")
U2 = UUID("00000000-0000-0000-0000-000000000002")


def test_parse_wildcard_path() -> None:
    assert LinkPath.parse("gallery[*].image").segments == ("gallery", "*", "image")


def test_parse_nested_wildcards_and_str_round_trip() -> None:
    path = LinkPath.parse("rows[*].cells[*].image")

    assert path.segments == ("rows", "*", "cells", "*", "image")
    assert str(path) == "rows[*].cells[*].image"


@pytest.mark.parametrize("bad", ["", "a..b", "a[0]", "a[*", "1a", "a.[*]", "[*]"])
def test_parse_rejects_invalid(bad: str) -> None:
    with pytest.raises(RegistryConfigurationError):
        LinkPath.parse(bad)


def test_iter_link_values_expands_lists_with_concrete_locations() -> None:
    data = {"gallery": [{"image": {"$blob": str(U1)}}, {"image": {"$blob": str(U2)}}]}

    found = list(iter_link_values(data, LinkPath.parse("gallery[*].image")))

    assert [loc for loc, _ in found] == ["gallery/0/image", "gallery/1/image"]
    assert found[0][1] == {"$blob": str(U1)}


def test_iter_link_values_missing_keys_yield_nothing() -> None:
    assert list(iter_link_values({}, LinkPath.parse("a[*].b"))) == []
    assert list(iter_link_values({"a": 1}, LinkPath.parse("a[*].b"))) == []
    assert list(iter_link_values({"a": [{"c": 1}]}, LinkPath.parse("a[*].b"))) == []


def test_reference_round_trip() -> None:
    assert ContentReference.from_json(ContentReference(U1).to_json()).target == U1


def test_blob_round_trip() -> None:
    assert BlobReference.from_json(BlobReference(U1).to_json()).blob_id == U1


@pytest.mark.parametrize(
    "raw",
    [
        str(U1),
        {"$ref": "nope"},
        {"$ref": str(U1), "x": 1},
        {"ref": str(U1)},
        {"$ref": 5},
        {"$ref": "ABCDEFAB-0000-0000-0000-000000000001"},
    ],
)
def test_reference_from_json_rejects(raw: object) -> None:
    with pytest.raises(SchemaDecodeError):
        ContentReference.from_json(raw)


def test_blob_from_json_rejects_reference_object() -> None:
    with pytest.raises(SchemaDecodeError):
        BlobReference.from_json({"$ref": str(U1)})


def test_find_undeclared_links_reports_extra_ref() -> None:
    assert find_undeclared_links({"a": {"$ref": str(U1)}}, []) == ("a",)


def test_find_undeclared_links_ignores_declared_and_reports_others() -> None:
    data = {
        "gallery": [{"image": {"$blob": str(U1)}}],
        "other": [{"$ref": str(U2)}],
    }

    assert find_undeclared_links(data, [LinkPath.parse("gallery[*].image")]) == (
        "other/0",
    )


def test_reference_field_validates_cardinality() -> None:
    types = frozenset({ContentTypeKey("a.b")})
    path = LinkPath.parse("x")
    with pytest.raises(RegistryConfigurationError):
        ReferenceFieldDefinition(path, frozenset())
    with pytest.raises(RegistryConfigurationError):
        ReferenceFieldDefinition(path, types, many=True, min_items=3, max_items=2)
    with pytest.raises(RegistryConfigurationError):
        ReferenceFieldDefinition(path, types, min_items=1)


def test_link_path_for_many_adds_wildcard() -> None:
    types = frozenset({ContentTypeKey("a.b")})

    many = ReferenceFieldDefinition(LinkPath.parse("x"), types, many=True)

    assert many.link_path == LinkPath.parse("x[*]")
    assert BlobFieldDefinition(LinkPath.parse("x")).link_path == LinkPath.parse("x")
