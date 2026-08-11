from __future__ import annotations

import pytest

from csbox.api.errors import ApiConfigError
from csbox.api.variables import interpolate, resolve_variables


def test_resolve_variables_uses_documented_precedence_and_reports_missing_immutably() -> None:
    resolution = resolve_variables(
        {"base_url", "token", "fallback", "missing"},
        project={"base_url": "https://project.test", "token": "project-token"},
        scenario={"base_url": "https://scenario.test"},
        environ={
            "base_url": "https://environment.test",
            "token": "environment-token",
            "CSBOX_VAR_fallback": "fallback-value",
        },
        cli={"base_url": "https://cli.test"},
    )

    assert dict(resolution.values) == {
        "base_url": "https://cli.test",
        "token": "environment-token",
        "fallback": "fallback-value",
    }
    assert resolution.missing_names == frozenset({"missing"})
    assert "missing" in resolution.user_message
    assert "在哪里" in resolution.user_message
    with pytest.raises(TypeError):
        resolution.values["token"] = "replacement"  # type: ignore[index]


def test_resolve_variables_positional_arguments_follow_the_public_signature() -> None:
    resolution = resolve_variables(
        {"environment_wins", "scenario_wins", "cli_wins"},
        {
            "environment_wins": "project",
            "scenario_wins": "project",
            "cli_wins": "project",
        },
        {"scenario_wins": "scenario", "cli_wins": "scenario"},
        {
            "environment_wins": "environment",
            "scenario_wins": "environment",
            "cli_wins": "environment",
        },
        {"cli_wins": "cli"},
    )

    assert dict(resolution.values) == {
        "environment_wins": "environment",
        "scenario_wins": "scenario",
        "cli_wins": "cli",
    }


@pytest.mark.parametrize("source", ["scenario", "environ", "cli"])
@pytest.mark.parametrize(
    "source_value",
    ["{{TODO_token}}", "TODO_password", "{{unresolved}}", "prefix-{{unresolved}}"],
)
def test_resolve_variables_keeps_unresolved_source_values_out_of_resolved_values(
    source: str, source_value: str
) -> None:
    sources: dict[str, dict[str, str]] = {
        "project": {},
        "scenario": {},
        "environ": {},
        "cli": {},
    }
    sources[source]["token"] = source_value

    resolution = resolve_variables(
        {"token"},
        sources["project"],
        sources["scenario"],
        sources["environ"],
        sources["cli"],
    )

    assert dict(resolution.values) == {}
    assert resolution.missing_names == frozenset({"token"})
    assert "{{" not in "".join(resolution.values.values())


@pytest.mark.parametrize("source", ["scenario", "environ", "cli"])
def test_resolve_variables_rejects_unsupported_source_templates_without_exposing_value(
    source: str,
) -> None:
    secret = "CSBOX_SECRET_SENTINEL_unsupported_source"
    source_value = f"{{{{__import__('os').system('whoami')}}}}-{secret}"
    sources: dict[str, dict[str, str]] = {
        "project": {},
        "scenario": {},
        "environ": {},
        "cli": {},
    }
    sources[source]["token"] = source_value

    with pytest.raises(ApiConfigError) as error:
        resolve_variables(
            {"token"},
            sources["project"],
            sources["scenario"],
            sources["environ"],
            sources["cli"],
        )

    assert secret not in str(error.value)
    assert "whoami" not in str(error.value)


def test_interpolate_recurses_without_changing_non_string_json_values() -> None:
    value = {
        "url": "{{base_url}}/health",
        "items": ["{{name}}", 1, True, None, ("{{name}}", 2.5)],
    }

    interpolated = interpolate(value, {"base_url": "https://example.test", "name": "CSBox"})

    assert interpolated == {
        "url": "https://example.test/health",
        "items": ["CSBox", 1, True, None, ("CSBox", 2.5)],
    }
    assert interpolated is not value


@pytest.mark.parametrize("template", ["{{missing}}", "{{TODO_password}}"])
def test_interpolate_rejects_missing_and_todo_placeholders_without_exposing_values(
    template: str,
) -> None:
    secret = "CSBOX_SECRET_SENTINEL_9f4d"

    with pytest.raises(ApiConfigError) as error:
        interpolate(template, {"password": secret})

    message = str(error.value)
    assert "发生了什么" in message
    assert "怎么处理" in message
    assert secret not in message


def test_interpolate_rejects_expression_like_placeholders() -> None:
    with pytest.raises(ApiConfigError) as error:
        interpolate("{{__import__('os').system('whoami')}}", {})

    assert "不支持" in str(error.value) or "安全" in str(error.value)


def test_interpolate_rejects_non_string_variable_values_with_a_safe_error() -> None:
    with pytest.raises(ApiConfigError) as error:
        interpolate("{{token}}", {"token": 1})  # type: ignore[dict-item]

    assert "类型" in str(error.value)


@pytest.mark.parametrize(
    "replacement",
    [
        "{{TODO_token}}",
        "TODO_password",
        "{{unresolved}}",
        "prefix-{{unresolved}}",
        "{{__import__('os')}}",
    ],
)
def test_interpolate_never_leaves_source_placeholder_residue(replacement: str) -> None:
    with pytest.raises(ApiConfigError) as error:
        interpolate("Bearer {{token}}", {"token": replacement})

    assert replacement not in str(error.value)
