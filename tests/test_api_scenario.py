from __future__ import annotations

from pathlib import Path

import pytest

from csbox.api.errors import ApiConfigError
from csbox.api.scenario import ScenarioLoader


def _write_scenario(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "login.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_loader_converts_typed_toml_and_preserves_json_value_types(tmp_path: Path) -> None:
    path = _write_scenario(
        tmp_path,
        """
name = "登录场景"

[variables]
base_url = "https://api.example.test"
token = "{{TODO_token}}"

[[steps]]
name = "登录"
method = "POST"
url = "{{base_url}}/login"
headers = { Accept = "application/json" }
query = { trace = "true" }
bearer = "{{token}}"
timeout = 2.5
follow_redirects = false
verify_tls = true

[steps.json]
email = "student@example.test"
password = "{{PASSWORD}}"
remember = true
attempt = 2
metadata = { active = false, tags = ["csbox", 2] }

[[steps.assertions]]
type = "status"
expected = 200

[[steps.assertions]]
type = "json_path"
path = "$.code"
expected = 200

[[steps]]
name = "上传"
method = "POST"
url = "{{base_url}}/upload"
timeout = 3

[[steps.multipart]]
name = "description"
value = "实验报告"

[[steps.multipart]]
name = "attachment"
value = "{{file_name}}"
""",
    )

    scenario = ScenarioLoader().load(path)
    login, upload = scenario.steps

    assert scenario.name == "登录场景"
    assert scenario.source == str(path)
    assert scenario.variables == {
        "base_url": "https://api.example.test",
        "token": "{{TODO_token}}",
    }
    assert login.request.method == "POST"
    assert login.request.headers["Authorization"] == "Bearer {{token}}"
    assert login.request.timeout_seconds == 2.5
    assert login.request.json_body == {
        "email": "student@example.test",
        "password": "{{PASSWORD}}",
        "remember": True,
        "attempt": 2,
        "metadata": {"active": False, "tags": ["csbox", 2]},
    }
    assert login.assertions[1].kind == "json_path"
    assert login.assertions[1].location == "$.code"
    assert upload.request.model_dump(mode="json")["multipart"] == [
        {"name": "description", "value": "实验报告"},
        {"name": "attachment", "value": "{{file_name}}"},
    ]
    assert upload.request.timeout_seconds == 3.0


@pytest.mark.parametrize(
    "content",
    [
        """
name = "坏方法"
[[steps]]
name = "危险步骤"
method = "TRACE"
url = "https://example.test"
""",
        """
name = "冲突 body"
[[steps]]
name = "提交"
method = "POST"
url = "https://example.test"
[steps.json]
ok = true
[steps.form]
name = "CSBox"
""",
        """
name = "小写方法"
[[steps]]
name = "小写"
method = "post"
url = "https://example.test"
""",
        """
name = "重复断言"
[[steps]]
name = "状态"
method = "GET"
url = "https://example.test"
[[steps.assertions]]
type = "status"
expected = 200
[[steps.assertions]]
type = "status"
expected = 200
""",
        """
name = "脚本"
[[steps]]
name = "危险"
method = "GET"
url = "{{__import__('os').system('whoami')}}"
""",
    ],
)
def test_loader_rejects_unsafe_schema_with_path_step_and_safe_chinese_repair_message(
    tmp_path: Path, content: str
) -> None:
    path = _write_scenario(tmp_path, content)

    with pytest.raises(ApiConfigError) as error:
        ScenarioLoader().load(path)

    message = str(error.value)
    assert str(path) in message
    assert "steps[0]" in message
    assert "发生了什么" in message
    assert "在哪里" in message
    assert "怎么处理" in message
    assert "whoami" not in message


def test_loader_uses_stable_step_index_without_step_name_or_variable_value(tmp_path: Path) -> None:
    step_secret = "CSBOX_SECRET_SENTINEL_step_name"
    value_secret = "CSBOX_SECRET_SENTINEL_variable_value"
    path = _write_scenario(
        tmp_path,
        f'''
name = "安全错误定位"
[[steps]]
name = "{step_secret}"
method = "TRACE"
url = "https://example.test/{{{{{value_secret}}}}}"
''',
    )

    with pytest.raises(ApiConfigError) as error:
        ScenarioLoader().load(path)

    message = str(error.value)
    assert str(path) in message
    assert "steps[0]" in message
    assert step_secret not in message
    assert value_secret not in message


def test_loader_rejects_bearer_and_explicit_authorization_together(tmp_path: Path) -> None:
    path = _write_scenario(
        tmp_path,
        """
name = "认证冲突"
[[steps]]
name = "认证"
method = "GET"
url = "https://example.test"
headers = { Authorization = "Basic hidden" }
bearer = "{{token}}"
""",
    )

    with pytest.raises(ApiConfigError) as error:
        ScenarioLoader().load(path)

    assert "Authorization" in str(error.value)


def test_loader_wraps_malformed_toml_in_chinese_actionable_error(tmp_path: Path) -> None:
    path = _write_scenario(tmp_path, 'name = "坏场景"\n[[steps]\nmethod = "GET"\n')

    with pytest.raises(ApiConfigError) as error:
        ScenarioLoader().load(path)

    message = str(error.value)
    assert str(path) in message
    assert "发生了什么" in message
    assert "在哪里" in message
    assert "怎么处理" in message
    assert "行" in message


def test_loader_accepts_only_the_documented_first_version_assertion_types(
    tmp_path: Path,
) -> None:
    path = _write_scenario(
        tmp_path,
        """
name = "断言类型"
[[steps]]
name = "响应"
method = "GET"
url = "https://example.test"
[[steps.assertions]]
type = "status"
expected = 200
[[steps.assertions]]
type = "header"
path = "Content-Type"
expected = "application/json"
[[steps.assertions]]
type = "json_path"
path = "$.status"
expected = "ok"
[[steps.assertions]]
type = "body"
expected = "healthy"
""",
    )

    scenario = ScenarioLoader().load(path)

    assert tuple(assertion.kind for assertion in scenario.steps[0].assertions) == (
        "status",
        "header",
        "json_path",
        "body",
    )


@pytest.mark.parametrize("assertion_type", ["run_shell", "script", "unknown", "STATUS"])
def test_loader_rejects_assertions_outside_first_version_allowlist(
    tmp_path: Path, assertion_type: str
) -> None:
    path = _write_scenario(
        tmp_path,
        f"""
name = "危险断言"
[[steps]]
name = "响应"
method = "GET"
url = "https://example.test"
[[steps.assertions]]
type = "{assertion_type}"
expected = "CSBOX_SECRET_SENTINEL_assertion"
""",
    )

    with pytest.raises(ApiConfigError) as error:
        ScenarioLoader().load(path)

    message = str(error.value)
    assert assertion_type not in message
    assert "CSBOX_SECRET_SENTINEL_assertion" not in message
    assert "发生了什么" in message
    assert "怎么处理" in message
