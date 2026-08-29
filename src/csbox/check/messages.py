from __future__ import annotations

from csbox.check.models import CheckFinding, CheckStatus

README_MISSING_MESSAGE = (
    "未找到 README。\n"
    "为什么：README 是提交者了解项目、运行方式和结果的入口。\n"
    "建议：在项目根目录添加 README.md，写明项目用途、运行命令和提交说明。"
)
ENV_FILE_FAIL_MESSAGE = (
    "发现真实 .env 文件。\n"
    "为什么：.env 可能包含本地凭据，提交后会随项目一起泄露。\n"
    "建议：提交前移除 .env；需要示例配置时保留不含真实凭据的 .env.example。"
)
PRIVATE_KEY_FAIL_MESSAGE = (
    "发现私钥内容。\n"
    "为什么：私钥一旦交付会允许冒用对应身份。\n"
    "建议：从提交内容移除私钥；如果已经提交，立即撤销或轮换对应密钥。"
)
ARTIFACT_WARN_MESSAGE = (
    "发现可能不应提交的构建、缓存或日志产物。\n"
    "为什么：这些文件会让提交变大，且可能包含本机或运行信息。\n"
    "建议：删除该路径或将其加入项目忽略规则后再检查。"
)
LARGE_FILE_WARN_MESSAGE = (
    "文件超过配置的大小阈值。\n"
    "为什么：大文件可能不符合课程提交要求或会拖慢交付。\n"
    "建议：移除生成文件；若确实需要，先确认课程提交要求。"
)
WINDOWS_ABSOLUTE_PATH_WARN_MESSAGE = (
    "发现 Windows 本机绝对路径引用。\n"
    "为什么：该路径只在当前电脑有效，其他人无法复现。\n"
    "建议：改用项目相对路径或配置/环境变量，并在 README 说明。"
)
UNIX_ABSOLUTE_PATH_WARN_MESSAGE = (
    "发现 Unix 本机绝对路径引用。\n"
    "为什么：该路径只在当前电脑有效，其他人无法复现。\n"
    "建议：改用项目相对路径或配置/环境变量，并在 README 说明。"
)
HARD_CODED_SECRET_FAIL_MESSAGE = (
    "发现可能的硬编码 secret；仅报告位置，不输出匹配内容。\n"
    "为什么：固定凭据可能随代码提交并被滥用。\n"
    "建议：删除固定值，改为从环境变量或本地未提交配置读取；示例文件请使用明确占位值。"
)
GIT_TOO_LARGE_WARN_MESSAGE = (
    "Git 状态输出过大，无法可靠判断工作区状态。\n"
    "为什么：当前结果可能没有覆盖所有变更。\n"
    "建议：在提交前直接检查 git status 和 git diff，再重新运行检查。"
)
GIT_DIRTY_WARN_MESSAGE = (
    "Git 工作区存在已修改文件。\n"
    "为什么：未确认变更可能导致提交内容与预期不一致。\n"
    "建议：检查差异，提交需要的文件或清理不应交付的改动后再检查。"
)
GIT_UNTRACKED_WARN_MESSAGE = (
    "Git 工作区存在未跟踪文件。\n"
    "为什么：未跟踪文件可能被遗漏，也可能包含临时或敏感内容。\n"
    "建议：检查这些文件，决定加入版本控制或移除后再检查。"
)
DEEP_SECRET_FAIL_MESSAGE = (
    "gitleaks 深度扫描发现疑似 secret。\n"
    "为什么：项目或 Git 历史中可能存在凭据暴露。\n"
    "建议：查看 gitleaks 的脱敏报告，删除并轮换凭据后再检查。"
)
DEEP_SCAN_INCOMPLETE_MESSAGE = (
    "gitleaks 深度扫描未完成，工具无法可靠执行。\n"
    "为什么：本次结果不能证明项目没有 secret。\n"
    "建议：检查 gitleaks 安装、项目权限和运行环境后重试。"
)

SAFE_SENSITIVE_MESSAGES = {
    "env": ENV_FILE_FAIL_MESSAGE,
    "private-key": PRIVATE_KEY_FAIL_MESSAGE,
    "hard-coded-secret": HARD_CODED_SECRET_FAIL_MESSAGE,
    "deep-secret-scan": DEEP_SECRET_FAIL_MESSAGE,
}

_SENSITIVE_RULE_TO_CATEGORY = {
    "env-file": "env",
    "private-key": "private-key",
    "hard-coded-secret": "hard-coded-secret",
    "deep-secret-scan": "deep-secret-scan",
}

_SENSITIVE_TITLES = {
    "env": "发现真实 .env 文件",
    "private-key": "发现私钥内容",
    "hard-coded-secret": "发现可能的硬编码 secret",
    "deep-secret-scan": "深度 secret 扫描发现疑似 secret",
}

_FINDING_TITLES = {
    "readme": "缺少 README",
    "artifacts": "发现构建、缓存或日志产物",
    "large-file": "文件超过大小阈值",
    "local-absolute-path": "发现本机绝对路径引用",
}

_CATEGORY_TITLES = {
    "windows-absolute-path": "发现 Windows 本机绝对路径引用",
    "unix-absolute-path": "发现 Unix 本机绝对路径引用",
    "dirty": "Git 工作区有已修改文件",
    "untracked": "Git 工作区有未跟踪文件",
}


def _sensitive_category(finding: CheckFinding) -> str | None:
    if finding.category in SAFE_SENSITIVE_MESSAGES:
        return finding.category
    return _SENSITIVE_RULE_TO_CATEGORY.get(finding.rule_id)


def public_finding_message(finding: CheckFinding) -> str:
    """Return a safe human-facing message without arbitrary sensitive content."""
    if finding.status is CheckStatus.FAIL:
        return SAFE_SENSITIVE_MESSAGES.get(_sensitive_category(finding), finding.message)
    return finding.message


def finding_title(finding: CheckFinding) -> str:
    if finding.status is CheckStatus.FAIL:
        sensitive_category = _sensitive_category(finding)
        if sensitive_category in _SENSITIVE_TITLES:
            return _SENSITIVE_TITLES[sensitive_category]
    if finding.category in _CATEGORY_TITLES:
        return _CATEGORY_TITLES[finding.category]
    if finding.rule_id in _FINDING_TITLES:
        return _FINDING_TITLES[finding.rule_id]
    message_lines = public_finding_message(finding).splitlines()
    return message_lines[0] if message_lines else finding.rule_id


__all__ = [
    "ARTIFACT_WARN_MESSAGE",
    "DEEP_SCAN_INCOMPLETE_MESSAGE",
    "DEEP_SECRET_FAIL_MESSAGE",
    "ENV_FILE_FAIL_MESSAGE",
    "GIT_DIRTY_WARN_MESSAGE",
    "GIT_TOO_LARGE_WARN_MESSAGE",
    "GIT_UNTRACKED_WARN_MESSAGE",
    "HARD_CODED_SECRET_FAIL_MESSAGE",
    "LARGE_FILE_WARN_MESSAGE",
    "PRIVATE_KEY_FAIL_MESSAGE",
    "README_MISSING_MESSAGE",
    "SAFE_SENSITIVE_MESSAGES",
    "UNIX_ABSOLUTE_PATH_WARN_MESSAGE",
    "WINDOWS_ABSOLUTE_PATH_WARN_MESSAGE",
    "finding_title",
    "public_finding_message",
]
