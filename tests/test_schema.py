from csbox.core.schema import SCHEMA_VERSION, with_schema_version


def test_machine_payload_has_schema_version() -> None:
    assert SCHEMA_VERSION == 1
    assert with_schema_version({"status": "PASS"}) == {
        "schema_version": 1,
        "status": "PASS",
    }


def test_schema_version_overwrites_incoming_value() -> None:
    payload = with_schema_version({"schema_version": 99, "status": "PASS"})

    assert list(payload) == ["schema_version", "status"]
    assert payload == {
        "schema_version": 1,
        "status": "PASS",
    }
