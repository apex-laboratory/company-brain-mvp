from __future__ import annotations

import pytest

from app.shared.helpers.ids import PREFIX_MAP, generate_id


def test_generate_id_user_prefix() -> None:
    new_id = generate_id("user")
    assert new_id.startswith("usr_")


def test_generate_id_is_lowercase_ulid() -> None:
    new_id = generate_id("workspace")
    prefix, _, ulid_part = new_id.partition("_")
    assert prefix == "wrk"
    assert ulid_part == ulid_part.lower()
    assert len(ulid_part) == 26  # ULID canonical length


def test_generate_id_unique() -> None:
    assert generate_id("user") != generate_id("user")


@pytest.mark.parametrize("entity", sorted(PREFIX_MAP))
def test_every_entity_maps_to_its_prefix(entity: str) -> None:
    assert generate_id(entity).startswith(f"{PREFIX_MAP[entity]}_")


def test_unknown_entity_raises() -> None:
    with pytest.raises(KeyError):
        generate_id("nope")
