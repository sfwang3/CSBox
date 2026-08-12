from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from csbox.api.errors import ApiPersistenceError
from csbox.api.evidence import ApiEvidenceBuilder
from csbox.api.models import (
    ApiAssertion,
    ApiAssertionResult,
    ApiRequest,
    ApiResponse,
    ApiRun,
    ApiRunResult,
    ApiScenario,
    ApiStep,
)

SECRET = "CSBOX_SECRET_SENTINEL_task10"
MASK = "••••••••"


def _request(name: str) -> ApiRequest:
    return ApiRequest(
        method="POST",
        url=(
            f"https://student:{SECRET}@example.test/中文路径/{name}"
            f"?access_token={SECRET}&q=中文查询"
        ),
        query={"q": "中文查询", "token": SECRET},
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {SECRET}",
            "X-Long-Header": "header-" + ("中" * 80),
        },
        json_body={
            "profile": {"name": "中文用户", "password": SECRET},
            "items": [{"token": SECRET, "value": "可见内容"}],
        },
        body=f"password={SECRET}&message=中文请求",
    )


def _response(status_code: int, body: str) -> ApiResponse:
    return ApiResponse(
        status_code=status_code,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Set-Cookie": f"session={SECRET}",
            "X-Response": "响应头中文" + ("-long" * 20),
        },
        body=body,
        url=f"https://example.test/中文结果?refresh_token={SECRET}",
        elapsed_ms=84.5,
        content_type="application/json; charset=utf-8",
        response_size=len(body.encode()),
    )


def _run() -> ApiRun:
    pass_assertion = ApiAssertion(kind="status", expected=200)
    fail_assertion = ApiAssertion(kind="status", expected=200)
    skip_assertion = ApiAssertion(kind="body", expected="never")
    response_body = json.dumps(
        {
            "code": 200,
            "message": "成功 中文",
            "nested": {"items": [{"password": SECRET, "label": "深层中文"}]},
            "long": "长响应" * 1800,
        },
        ensure_ascii=False,
    )
    steps = (
        ApiStep(name="登录成功", request=_request("login"), assertions=(pass_assertion,)),
        ApiStep(name=f"失败步骤-{SECRET}", request=_request("fail"), assertions=(fail_assertion,)),
        ApiStep(name="网络错误", request=_request("runtime"), assertions=(skip_assertion,)),
    )
    results = (
        ApiRunResult(
            step_name="登录成功",
            status="PASS",
            response=_response(200, response_body),
            assertions=(
                ApiAssertionResult(
                    assertion=pass_assertion,
                    status="PASS",
                    message="状态码符合预期",
                    actual=200,
                ),
            ),
            elapsed_ms=84.5,
        ),
        ApiRunResult(
            step_name=f"失败步骤-{SECRET}",
            status="FAIL",
            response=_response(500, '{"code":500,"message":"失败 中文"}'),
            assertions=(
                ApiAssertionResult(
                    assertion=fail_assertion,
                    status="FAIL",
                    message=f"实际值包含 {SECRET}，位置为 status",
                    actual=500,
                ),
            ),
            elapsed_ms=12.0,
        ),
        ApiRunResult(
            step_name="网络错误",
            status="RUNTIME_ERROR",
            response=None,
            assertions=(
                ApiAssertionResult(
                    assertion=skip_assertion,
                    status="SKIP",
                    message=f"未收到响应 {SECRET}",
                ),
            ),
            error=f"连接失败 {SECRET}",
            elapsed_ms=1000.0,
        ),
    )
    return ApiRun(
        id=f"run-{SECRET}",
        scenario=ApiScenario(
            name="中文 API 场景",
            source=str(Path("scenarios") / "中文场景.toml"),
            variables={"token": SECRET},
            steps=steps,
        ),
        started_at=datetime(2026, 8, 11, 10, 0, tzinfo=UTC),
        ended_at=datetime(2026, 8, 11, 10, 0, 2, tzinfo=UTC),
        status="RUNTIME_ERROR",
        results=results,
        elapsed_ms=1096.5,
    )


def test_builder_creates_one_safe_evidence_item_per_executed_step() -> None:
    run = _run()

    evidence = ApiEvidenceBuilder.from_run(run)

    assert len(evidence) == 3
    assert [item.step_index for item in evidence] == [1, 2, 3]
    assert [item.result.status for item in evidence if item.result is not None] == [
        "PASS",
        "FAIL",
        "RUNTIME_ERROR",
    ]
    assert evidence[0].scenario_name == "中文 API 场景"
    assert evidence[0].run_status == "RUNTIME_ERROR"
    assert evidence[2].response is None
    assert evidence[2].result is not None
    assert evidence[2].result.assertions[0].status == "SKIP"

    for item in evidence:
        assert SECRET not in item.model_dump_json()
        assert MASK in item.model_dump_json()

    assert SECRET in run.model_dump_json()


def test_evidence_items_are_immutable_public_views() -> None:
    item = ApiEvidenceBuilder.from_run(_run())[0]

    with pytest.raises((TypeError, ValueError)):
        item.title = "改写"  # type: ignore[misc]


def test_builder_does_not_mutate_the_input_run() -> None:
    run = _run()
    before = run.model_dump_json()

    ApiEvidenceBuilder.from_run(run)

    assert run.model_dump_json() == before


def test_builder_collects_request_and_response_secrets_without_variable_map() -> None:
    assertion = ApiAssertion(kind="status", expected=200)
    request = ApiRequest(
        method="GET",
        url=f"https://user:{SECRET}@example.test/中文?token={SECRET}",
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    response = ApiResponse(
        status_code=200,
        body=json.dumps({"token": SECRET, "echo": SECRET}),
        url=request.url,
        content_type="application/json",
    )
    result = ApiRunResult(
        step_name=f"标题-{SECRET}",
        status="PASS",
        response=response,
        assertions=(
            ApiAssertionResult(
                assertion=assertion,
                status="PASS",
                message=f"通过 {SECRET}",
            ),
        ),
    )
    run = ApiRun(
        id="safe-collection",
        scenario=ApiScenario(
            name="场景",
            steps=(ApiStep(name=f"标题-{SECRET}", request=request, assertions=(assertion,)),),
        ),
        started_at=datetime(2026, 8, 11, tzinfo=UTC),
        status="PASS",
        results=(result,),
    )

    item = ApiEvidenceBuilder.from_run(run)[0]

    assert SECRET not in item.model_dump_json()
    assert MASK in item.title
    assert item.result is not None
    assert MASK in item.result.assertions[0].message


def test_builder_rejects_a_corrupt_run_with_more_results_than_steps() -> None:
    run = _run()
    corrupt = run.model_copy(update={"results": run.results + (run.results[-1],)})

    with pytest.raises(ApiPersistenceError, match="运行记录|证据"):
        ApiEvidenceBuilder.from_run(corrupt)
