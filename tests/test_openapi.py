from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import csbox.api.openapi as openapi_module
import csbox.api.scenario_writer as writer_module
from csbox.api.errors import ApiConfigError
from csbox.api.models import ApiRequest, ApiStep
from csbox.api.openapi import OpenApiImporter, write_scenario_templates
from csbox.api.scenario import ScenarioLoader
from csbox.cli.main import app


def _write_json_spec(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "Pets", "version": "1"},
                "servers": [{"url": "https://api.example.test/v1"}],
                "paths": {
                    "/pets/{petId}": {
                        "parameters": [
                            {
                                "name": "petId",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                        "get": {
                            "operationId": "getPet",
                            "parameters": [
                                {
                                    "name": "include",
                                    "in": "query",
                                    "schema": {"default": "owner"},
                                },
                                {
                                    "name": "X-Trace-Id",
                                    "in": "header",
                                    "schema": {"type": "string"},
                                },
                            ],
                            "responses": {
                                "200": {"description": "ok"},
                                "404": {"description": "missing"},
                            },
                        },
                        "post": {
                            "operationId": "updatePet",
                            "requestBody": {
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "name": {"type": "string"},
                                                "metadata": {
                                                    "type": "object",
                                                    "properties": {"active": {"default": True}},
                                                },
                                                "tags": {
                                                    "type": "array",
                                                    "items": {"type": "string"},
                                                },
                                            },
                                        }
                                    }
                                }
                            },
                            "responses": {
                                "200": {"description": "updated"},
                                "422": {"description": "invalid"},
                            },
                        },
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_importer_loads_json_and_creates_deterministic_safe_templates(tmp_path: Path) -> None:
    source = tmp_path / "pets.json"
    _write_json_spec(source)

    document = OpenApiImporter().load(source)
    scenario = OpenApiImporter().to_scenario(document, "pets")

    assert document.version == "3.0.3"
    assert [step.name for step in scenario.steps] == ["getPet", "updatePet"]
    get_request, post_request = (step.request for step in scenario.steps)
    assert get_request.url == "https://api.example.test/v1/pets/{{TODO_petId}}"
    assert get_request.query == {"include": "owner"}
    assert get_request.headers == {"X-Trace-Id": "{{TODO_X_Trace_Id}}"}
    assert post_request.json_body == {
        "name": "{{TODO_name}}",
        "metadata": {"active": True},
        "tags": ["{{TODO_tags_item}}"],
    }
    assert all(step.assertions == () for step in scenario.steps)

    destination = tmp_path / "scenarios"
    written = write_scenario_templates(scenario, destination)

    assert [path.name for path in written] == ["getPet.toml", "updatePet.toml"]
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in written)
    assert "已声明响应状态：200, 404" in rendered
    assert "已声明响应状态：200, 422" in rendered
    assert "assertions = []" in rendered
    assert "password" not in rendered.casefold()
    assert "student" not in rendered.casefold()
    assert ScenarioLoader().load(written[0]).steps[0].assertions == ()
    assert ScenarioLoader().load(written[1]).steps[0].request.json_body == post_request.json_body


def test_importer_loads_openapi_31_yaml_and_rejects_invalid_or_unsafe_documents(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "health.yaml"
    yaml_path.write_text(
        """openapi: 3.1.0
info:
  title: Health
  version: '1'
paths:
  /health:
    get:
      operationId: health
      responses:
        '200':
          description: healthy
""",
        encoding="utf-8",
    )
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text('{"openapi":"2.0","paths":{}}', encoding="utf-8")
    unsafe_path = tmp_path / "unsafe.yaml"
    unsafe_path.write_text("openapi: !!python/name:os.system ''\npaths: {}\n", encoding="utf-8")
    list_path = tmp_path / "list.yaml"
    list_path.write_text("- openapi: 3.1.0\n", encoding="utf-8")

    importer = OpenApiImporter()

    assert importer.to_scenario(importer.load(yaml_path), "health").steps[0].name == "health"
    for path in (invalid_path, unsafe_path, list_path):
        with pytest.raises(ApiConfigError):
            importer.load(path)


def test_importer_rejects_oversized_deep_and_aliased_yaml_with_bounded_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openapi_module, "MAX_OPENAPI_BYTES", 256, raising=False)
    oversized = tmp_path / "oversized.yaml"
    oversized.write_text(
        "openapi: 3.1.0\npaths: {}\n# " + ("x" * 512),
        encoding="utf-8",
    )
    with pytest.raises(ApiConfigError, match="读取|解析|大小|上限"):
        OpenApiImporter().load(oversized)

    monkeypatch.setattr(openapi_module, "MAX_OPENAPI_BYTES", 64 * 1024, raising=False)
    aliased = tmp_path / "aliased.yaml"
    aliased.write_text(
        """openapi: 3.1.0
paths:
  /health:
    get:
      responses: &responses
        '200': {description: ok}
      x-copy: *responses
""",
        encoding="utf-8",
    )
    with pytest.raises(ApiConfigError, match="安全解析"):
        OpenApiImporter().load(aliased)

    deep = tmp_path / "deep.json"
    value: object = "leaf"
    for _ in range(80):
        value = {"child": value}
    deep.write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "paths": {
                    "/health": {
                        "get": {
                            "responses": {"200": {"description": "ok"}},
                            "x-deep": value,
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ApiConfigError, match="安全解析"):
        OpenApiImporter().load(deep)


def test_template_publish_does_not_replace_a_destination_created_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "pets.json"
    _write_json_spec(source)
    importer = OpenApiImporter()
    scenario = importer.to_scenario(importer.load(source), "pets")
    destination = tmp_path / "scenarios"
    destination.mkdir()
    raced = destination / "getPet.toml"
    called = False

    from csbox.core.safe_paths import safe_rename as real_safe_rename

    def race_publish(
        source_path: Path, destination_path: Path, *, replace_existing: bool = True
    ) -> None:
        nonlocal called
        if destination_path == raced and not called:
            called = True
            raced.write_text("user-raced", encoding="utf-8")
        real_safe_rename(
            source_path,
            destination_path,
            replace_existing=replace_existing,
        )

    monkeypatch.setattr(writer_module, "safe_rename", race_publish, raising=False)

    with pytest.raises(FileExistsError):
        write_scenario_templates(scenario, destination)

    assert called
    assert raced.read_text(encoding="utf-8") == "user-raced"


def test_force_template_rollback_preserves_concurrent_destination_and_old_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "pets.json"
    _write_json_spec(source)
    importer = OpenApiImporter()
    scenario = importer.to_scenario(importer.load(source), "pets")
    destination = tmp_path / "scenarios"
    first = write_scenario_templates(scenario, destination)
    protected = first[0]
    old_generated = protected.read_text(encoding="utf-8")
    concurrent = "concurrent-user-data"
    real_safe_rename = writer_module.safe_rename
    raced = False

    def race_after_backup(
        source_path: Path, destination_path: Path, *, replace_existing: bool = True
    ) -> None:
        nonlocal raced
        if destination_path == protected and source_path.name == protected.name and not raced:
            raced = True
            assert not destination_path.exists()
            destination_path.write_text(concurrent, encoding="utf-8")
        real_safe_rename(
            source_path,
            destination_path,
            replace_existing=replace_existing,
        )

    monkeypatch.setattr(writer_module, "safe_rename", race_after_backup)

    with pytest.raises(FileExistsError):
        write_scenario_templates(scenario, destination, force=True)

    assert raced
    assert protected.read_text(encoding="utf-8") == old_generated
    recovery = tuple(destination.glob(".csbox-recovery-*.bak"))
    assert len(recovery) == 1
    assert recovery[0].read_text(encoding="utf-8") == concurrent
    assert not tuple(tmp_path.glob(".scenarios-*.partial"))


def test_force_template_restores_the_old_set_when_a_later_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "pets.json"
    _write_json_spec(source)
    importer = OpenApiImporter()
    scenario = importer.to_scenario(importer.load(source), "pets")
    destination = tmp_path / "scenarios"
    generated = write_scenario_templates(scenario, destination)
    old_contents = {path: path.read_bytes() for path in generated}
    first_step, *remaining_steps = scenario.steps
    updated_request = first_step.request.model_copy(
        update={"url": "https://api.example.test/v2/pets/{{TODO_petId}}"}
    )
    updated = scenario.model_copy(
        update={
            "steps": (
                first_step.model_copy(update={"request": updated_request}),
                *remaining_steps,
            )
        }
    )
    real_safe_rename = writer_module.safe_rename
    publish_calls = 0

    def fail_second_publish(
        source_path: Path, destination_path: Path, *, replace_existing: bool = True
    ) -> None:
        nonlocal publish_calls
        if destination_path in old_contents and source_path.name == destination_path.name:
            publish_calls += 1
            if publish_calls == 2:
                raise OSError("simulated later publication failure")
        real_safe_rename(
            source_path,
            destination_path,
            replace_existing=replace_existing,
        )

    monkeypatch.setattr(writer_module, "safe_rename", fail_second_publish)

    with pytest.raises(OSError, match="later publication failure"):
        write_scenario_templates(updated, destination, force=True)

    assert publish_calls == 2
    assert {path: path.read_bytes() for path in generated} == old_contents
    assert tuple(destination.glob(".csbox-recovery-*.bak"))
    assert not tuple(tmp_path.glob(".scenarios-*.partial"))


def test_template_import_rolls_back_new_files_when_mixed_publication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "pets.json"
    _write_json_spec(source)
    importer = OpenApiImporter()
    scenario = importer.to_scenario(importer.load(source), "pets")
    third = ApiStep(
        name="third",
        request=ApiRequest(method="GET", url="https://api.example.test/third"),
    )
    scenario = scenario.model_copy(update={"steps": (*scenario.steps, third)})
    destination = tmp_path / "scenarios"
    destination.mkdir()
    existing = destination / "getPet.toml"
    existing.write_text("old scenario", encoding="utf-8")
    real_safe_rename = writer_module.safe_rename

    def fail_third_publish(
        source_path: Path, destination_path: Path, *, replace_existing: bool = True
    ) -> None:
        if source_path.name == "third.toml" and destination_path.name == "third.toml":
            raise OSError("simulated mixed publication failure")
        real_safe_rename(
            source_path,
            destination_path,
            replace_existing=replace_existing,
        )

    monkeypatch.setattr(writer_module, "safe_rename", fail_third_publish)

    with pytest.raises(OSError, match="mixed publication failure"):
        write_scenario_templates(scenario, destination, force=True)

    assert existing.read_text(encoding="utf-8") == "old scenario"
    assert not (destination / "updatePet.toml").exists()
    assert not (destination / "third.toml").exists()
    assert not tuple(tmp_path.glob(".scenarios-*.partial"))


def test_template_import_preserves_invalid_utf8_concurrent_new_file_on_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "pets.json"
    _write_json_spec(source)
    importer = OpenApiImporter()
    scenario = importer.to_scenario(importer.load(source), "pets")
    third = ApiStep(
        name="third",
        request=ApiRequest(method="GET", url="https://api.example.test/third"),
    )
    scenario = scenario.model_copy(update={"steps": (*scenario.steps, third)})
    destination = tmp_path / "scenarios"
    destination.mkdir()
    existing = destination / "getPet.toml"
    existing.write_text("old scenario", encoding="utf-8")
    real_safe_rename = writer_module.safe_rename

    def mutate_new_file_then_fail(
        source_path: Path, destination_path: Path, *, replace_existing: bool = True
    ) -> None:
        if source_path.name == "third.toml" and destination_path.name == "third.toml":
            raise OSError("simulated invalid occupant publication failure")
        real_safe_rename(
            source_path,
            destination_path,
            replace_existing=replace_existing,
        )
        if source_path.name == "updatePet.toml" and destination_path.parent == destination:
            destination_path.write_bytes(b"\xff\xfe concurrent occupant")

    monkeypatch.setattr(writer_module, "safe_rename", mutate_new_file_then_fail)

    with pytest.raises(OSError, match="invalid occupant publication failure"):
        write_scenario_templates(scenario, destination, force=True)

    assert existing.read_text(encoding="utf-8") == "old scenario"
    assert not (destination / "updatePet.toml").exists()
    assert not (destination / "third.toml").exists()
    recovery = tuple(destination.glob(".csbox-recovery-*.bak"))
    assert any(path.read_bytes() == b"\xff\xfe concurrent occupant" for path in recovery)
    assert not tuple(tmp_path.glob(".scenarios-*.partial"))


def test_importer_rejects_duplicate_yaml_keys_and_nested_unsupported_schema(
    tmp_path: Path,
) -> None:
    duplicate_yaml = tmp_path / "duplicate.yaml"
    duplicate_yaml.write_text(
        """openapi: 3.1.0
paths:
  /health:
    get:
      responses:
        '200': {description: first}
        '200': {description: second}
""",
        encoding="utf-8",
    )
    reference_json = tmp_path / "reference.json"
    reference_json.write_text(
        json.dumps(
            {
                "openapi": "3.0.0",
                "paths": {
                    "/pets": {
                        "post": {
                            "requestBody": {
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "owner": {"$ref": "#/components/schemas/Owner"}
                                            },
                                        }
                                    }
                                }
                            },
                            "responses": {"200": {"description": "ok"}},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    composed_reference_json = tmp_path / "composed-reference.json"
    composed_reference_json.write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "paths": {
                    "/pets": {
                        "post": {
                            "requestBody": {
                                "content": {
                                    "application/json": {
                                        "schema": {"allOf": [{"$ref": "#/components/schemas/Pet"}]}
                                    }
                                }
                            },
                            "responses": {"200": {"description": "ok"}},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    non_scalar_key_yaml = tmp_path / "non-scalar-key.yaml"
    non_scalar_key_yaml.write_text(
        """openapi: 3.1.0
paths:
  ? [not, a, string]
  : get:
      responses:
        '200': {description: ok}
""",
        encoding="utf-8",
    )
    cyclic_alias_yaml = tmp_path / "cyclic-alias.yaml"
    cyclic_alias_yaml.write_text(
        """openapi: 3.1.0
paths: &paths
  /health: *paths
""",
        encoding="utf-8",
    )

    importer = OpenApiImporter()
    for path in (
        duplicate_yaml,
        reference_json,
        composed_reference_json,
        non_scalar_key_yaml,
        cyclic_alias_yaml,
    ):
        with pytest.raises(ApiConfigError):
            importer.load(path)


def test_importer_rejects_duplicate_parameters_and_template_overwrite_or_symlink(
    tmp_path: Path,
) -> None:
    duplicate_path = tmp_path / "duplicate.json"
    duplicate_path.write_text(
        json.dumps(
            {
                "openapi": "3.0.0",
                "paths": {
                    "/pets": {
                        "get": {
                            "parameters": [
                                {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                                {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                            ],
                            "responses": {"200": {"description": "ok"}},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    importer = OpenApiImporter()
    with pytest.raises(ApiConfigError):
        importer.load(duplicate_path)

    source = tmp_path / "pets.json"
    _write_json_spec(source)
    scenario = importer.to_scenario(importer.load(source), "pets")
    destination = tmp_path / "scenarios"
    written = write_scenario_templates(scenario, destination)
    with pytest.raises(FileExistsError):
        write_scenario_templates(scenario, destination)
    write_scenario_templates(scenario, destination, force=True)

    target = tmp_path / "protected.toml"
    target.write_text("protected", encoding="utf-8")
    written[0].unlink()
    written[0].symlink_to(target)
    with pytest.raises(ValueError):
        write_scenario_templates(scenario, destination, force=True)
    assert target.read_text(encoding="utf-8") == "protected"


def test_api_import_cli_emits_chinese_plain_or_schema_versioned_json(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "pets.json"
    _write_json_spec(source)
    monkeypatch.chdir(tmp_path)

    plain = CliRunner().invoke(app, ["api", "import", str(source), "--plain"])
    payload = CliRunner().invoke(
        app,
        ["api", "import", str(source), "--output", "custom", "--json"],
    )

    assert plain.exit_code == 0
    assert "已导入 2 个 OpenAPI 操作" in plain.stdout
    assert "scenarios" in plain.stdout
    assert payload.exit_code == 0
    assert json.loads(payload.stdout)["schema_version"] == 1
    assert json.loads(payload.stdout)["files"] == ["custom/getPet.toml", "custom/updatePet.toml"]


def test_importer_never_writes_sensitive_examples_or_server_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "access_token-secret.json"
    source.write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "servers": [
                    {
                        "url": (
                            "https://alice:super-server-password@api.example.test/v1"
                            "?access_token=server-token&safe=ok"
                        )
                    }
                ],
                "paths": {
                    "/sessions": {
                        "post": {
                            "operationId": "createAccessToken",
                            "parameters": [
                                {"name": "token", "in": "query", "example": "query-token"},
                                {
                                    "name": "Authorization",
                                    "in": "header",
                                    "schema": {"default": "Bearer header-token"},
                                },
                            ],
                            "requestBody": {
                                "content": {
                                    "application/json": {
                                        "example": {
                                            "password": "body-password",
                                            "profile": {
                                                "apiKey": "nested-api-key",
                                                "name": "Ada",
                                            },
                                        }
                                    }
                                }
                            },
                            "responses": {"200": {"description": "ok"}},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    importer = OpenApiImporter()
    scenario = importer.to_scenario(importer.load(source), source.stem)
    request = scenario.steps[0].request
    written = write_scenario_templates(scenario, tmp_path / "scenarios")
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in written)

    assert scenario.name == "openapi-import"
    assert scenario.steps[0].name == "operation-1"
    assert request.url == (
        "https://api.example.test/v1/sessions?access_token={{TODO_access_token}}&safe=ok"
    )
    assert request.query == {"token": "{{TODO_token}}"}
    assert request.headers == {"Authorization": "{{TODO_Authorization}}"}
    assert request.json_body == {
        "password": "{{TODO_password}}",
        "profile": {"apiKey": "{{TODO_apiKey}}", "name": "Ada"},
    }
    for secret in (
        "alice",
        "super-server-password",
        "server-token",
        "query-token",
        "header-token",
        "body-password",
        "nested-api-key",
        "createAccessToken",
        "access_token-secret",
    ):
        assert secret not in rendered
    assert [path.name for path in written] == ["operation-1.toml"]
    assert ScenarioLoader().load(written[0]).steps[0].request.json_body == request.json_body

    monkeypatch.chdir(tmp_path)
    plain = CliRunner().invoke(app, ["api", "import", str(source), "--output", "cli", "--plain"])
    payload = CliRunner().invoke(app, ["api", "import", str(source), "--output", "json", "--json"])

    assert plain.exit_code == 0
    assert payload.exit_code == 0
    assert "operation-1.toml" in plain.stdout
    assert json.loads(payload.stdout)["files"] == ["json/operation-1.toml"]
    for secret in ("createAccessToken", "access_token-secret", "server-token"):
        assert secret not in plain.stdout
        assert secret not in payload.stdout


def test_importer_rejects_non_string_request_content_key_and_non_finite_values(
    tmp_path: Path,
) -> None:
    non_string_content = tmp_path / "bad-content.yaml"
    non_string_content.write_text(
        """openapi: 3.1.0
paths:
  /pets:
    post:
      requestBody:
        content:
          1: {schema: {type: object}}
      responses: {'200': {description: ok}}
""",
        encoding="utf-8",
    )
    non_finite = tmp_path / "non-finite.json"
    non_finite.write_text(
        '{"openapi":"3.0.0","paths":{"/pets":{"post":{"requestBody":'
        '{"content":{"application/json":{"example":{"count":NaN}}}},'
        '"responses":{"200":{"description":"ok"}}}}}}',
        encoding="utf-8",
    )

    importer = OpenApiImporter()
    for path in (non_string_content, non_finite):
        with pytest.raises(ApiConfigError):
            importer.load(path)

    result = CliRunner().invoke(app, ["api", "import", str(non_string_content), "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["status"] == "CONFIG_ERROR"
    assert payload["exit_code"] == 2


def test_template_staging_failure_leaves_no_new_target_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "pets.json"
    _write_json_spec(source)
    importer = OpenApiImporter()
    scenario = importer.to_scenario(importer.load(source), "pets")
    destination = tmp_path / "scenarios"

    original_write = writer_module.atomic_write_text
    write_count = 0

    def fail_second_staging_write(path: Path, text: str) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OSError("injected staging write failure")
        original_write(path, text)

    monkeypatch.setattr(writer_module, "atomic_write_text", fail_second_staging_write)

    with pytest.raises(OSError, match="injected staging write failure"):
        write_scenario_templates(scenario, destination)

    assert not destination.exists()
    assert not tuple(tmp_path.glob(".scenarios-*.partial"))


def test_template_preflight_preserves_existing_files_before_any_publish_failure(
    tmp_path: Path,
) -> None:
    source = tmp_path / "pets.json"
    _write_json_spec(source)
    importer = OpenApiImporter()
    scenario = importer.to_scenario(importer.load(source), "pets")
    destination = tmp_path / "scenarios"
    destination.mkdir()
    protected = destination / "getPet.toml"
    protected.write_text("do not replace", encoding="utf-8")
    blocked = destination / "updatePet.toml"
    blocked.mkdir()

    with pytest.raises(ValueError):
        write_scenario_templates(scenario, destination, force=True)

    assert protected.read_text(encoding="utf-8") == "do not replace"
    assert blocked.is_dir()
