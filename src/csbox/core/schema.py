from collections.abc import Mapping

SCHEMA_VERSION = 1


def with_schema_version(payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        **{key: value for key, value in payload.items() if key != "schema_version"},
    }
