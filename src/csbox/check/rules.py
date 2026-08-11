from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Protocol

from csbox.check.detectors import PRUNED_DIRECTORIES
from csbox.check.models import CheckContext, CheckFinding, CheckStatus


class CheckRule(Protocol):
    rule_id: str

    def evaluate(self, context: CheckContext) -> tuple[CheckFinding, ...]: ...


def _finding(
    rule_id: str,
    status: CheckStatus,
    message: str,
    *,
    path: Path | None = None,
    line: int | None = None,
    category: str | None = None,
) -> CheckFinding:
    return CheckFinding(
        rule_id=rule_id,
        status=status,
        message=message,
        path=path,
        line=line,
        category=category,
    )


class ReadmeRule:
    rule_id = "readme"

    def evaluate(self, context: CheckContext) -> tuple[CheckFinding, ...]:
        names = {"readme", "readme.md", "readme.rst", "readme.txt"}
        found = next(
            (
                entry.relative
                for entry in context.inventory.files
                if entry.relative.parent == Path(".") and entry.relative.name.casefold() in names
            ),
            None,
        )
        if found is None:
            return (_finding(self.rule_id, CheckStatus.WARN, "未找到 README。", category="README"),)
        return (
            _finding(
                self.rule_id,
                CheckStatus.PASS,
                "README 已找到。",
                path=found,
                category="README",
            ),
        )


class EnvRule:
    rule_id = "env-file"

    def evaluate(self, context: CheckContext) -> tuple[CheckFinding, ...]:
        entries = [
            entry for entry in context.inventory.files if entry.relative.name.casefold() == ".env"
        ]
        if entries:
            return tuple(
                _finding(
                    self.rule_id,
                    CheckStatus.FAIL,
                    "发现真实 .env 文件，请在交付前移除。",
                    path=entry.relative,
                    category="env",
                )
                for entry in entries
            )
        return (_finding(self.rule_id, CheckStatus.PASS, "未发现真实 .env 文件。", category="env"),)


PRIVATE_KEY_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
)


class PrivateKeyRule:
    rule_id = "private-key"

    def evaluate(self, context: CheckContext) -> tuple[CheckFinding, ...]:
        findings: list[CheckFinding] = []
        for entry in context.inventory.files:
            name = entry.relative.name.casefold()
            likely_name = name.endswith((".pem", ".key", ".ppk")) or name in {
                "id_rsa",
                "id_ed25519",
                "id_ecdsa",
            }
            try:
                with entry.absolute.open("rb") as stream:
                    header = stream.read(512)
            except OSError:
                header = b""
            if (likely_name or header.startswith(b"-----BEGIN")) and any(
                marker in header for marker in PRIVATE_KEY_MARKERS
            ):
                findings.append(
                    _finding(
                        self.rule_id,
                        CheckStatus.FAIL,
                        "发现私钥文件。",
                        path=entry.relative,
                        category="private-key",
                    )
                )
        if not findings:
            return (
                _finding(
                    self.rule_id, CheckStatus.PASS, "未发现私钥文件。", category="private-key"
                ),
            )
        return tuple(findings)


class ArtifactRule:
    rule_id = "artifacts"
    names = (PRUNED_DIRECTORIES - {".git", ".csbox"}) | {
        "cache",
        "caches",
        "log",
        "logs",
    }

    def evaluate(self, context: CheckContext) -> tuple[CheckFinding, ...]:
        paths = [
            path for path in context.inventory.directories if path.name.casefold() in self.names
        ]
        paths.extend(
            entry.relative
            for entry in context.inventory.files
            if entry.relative.suffix.casefold() == ".log"
        )
        if not paths:
            return (
                _finding(
                    self.rule_id,
                    CheckStatus.PASS,
                    "未发现常见构建、缓存或日志产物。",
                    category="artifact",
                ),
            )
        return tuple(
            _finding(
                self.rule_id,
                CheckStatus.WARN,
                "发现可能不应提交的构建、缓存或日志产物。",
                path=path,
                category="artifact",
            )
            for path in sorted(set(paths), key=lambda item: item.as_posix().casefold())
        )


class LargeFileRule:
    rule_id = "large-file"

    def evaluate(self, context: CheckContext) -> tuple[CheckFinding, ...]:
        entries = [
            entry
            for entry in context.inventory.files
            if entry.size > context.large_file_threshold_bytes
        ]
        if not entries:
            return (
                _finding(self.rule_id, CheckStatus.PASS, "未发现超大文件。", category="large-file"),
            )
        return tuple(
            _finding(
                self.rule_id,
                CheckStatus.WARN,
                "文件超过配置的大小阈值。",
                path=entry.relative,
                category="large-file",
            )
            for entry in entries
        )


WINDOWS_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:\\")
UNIX_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_])/(?:home|Users|tmp|mnt|workspace|opt|var)/")
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?key|token|secret|password|passwd)\b"
    r"\s*[:=]\s*(['\"])(?P<value>[^'\"]{8,})\1"
)


class AbsolutePathRule:
    rule_id = "local-absolute-path"

    def evaluate(self, context: CheckContext) -> tuple[CheckFinding, ...]:
        findings: list[CheckFinding] = []
        for entry, content in context.inventory.text_files():
            for line_number, line in enumerate(content.splitlines(), start=1):
                if WINDOWS_ABSOLUTE_PATH.search(line):
                    findings.append(
                        _finding(
                            self.rule_id,
                            CheckStatus.WARN,
                            "发现 Windows 本机绝对路径引用。",
                            path=entry.relative,
                            line=line_number,
                            category="windows-absolute-path",
                        )
                    )
                if UNIX_ABSOLUTE_PATH.search(line):
                    findings.append(
                        _finding(
                            self.rule_id,
                            CheckStatus.WARN,
                            "发现 Unix 本机绝对路径引用。",
                            path=entry.relative,
                            line=line_number,
                            category="unix-absolute-path",
                        )
                    )
        return tuple(findings) or (
            _finding(
                self.rule_id, CheckStatus.PASS, "未发现本机绝对路径引用.", category="absolute-path"
            ),
        )


class HardCodedSecretRule:
    rule_id = "hard-coded-secret"

    def evaluate(self, context: CheckContext) -> tuple[CheckFinding, ...]:
        findings: list[CheckFinding] = []
        for entry, content in context.inventory.text_files():
            for line_number, line in enumerate(content.splitlines(), start=1):
                if SECRET_ASSIGNMENT.search(line):
                    findings.append(
                        _finding(
                            self.rule_id,
                            CheckStatus.FAIL,
                            "发现疑似硬编码 secret；仅报告位置，不输出匹配内容。",
                            path=entry.relative,
                            line=line_number,
                            category="hard-coded-secret",
                        )
                    )
        return tuple(findings) or (
            _finding(
                self.rule_id,
                CheckStatus.PASS,
                "未发现疑似硬编码 secret。",
                category="hard-coded-secret",
            ),
        )


class GitStatusRule:
    rule_id = "git-status"

    def evaluate(self, context: CheckContext) -> tuple[CheckFinding, ...]:
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(context.root),
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                ],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return (
                _finding(self.rule_id, CheckStatus.SKIP, "无法读取 Git 状态。", category="git"),
            )
        if result.returncode != 0:
            return (
                _finding(self.rule_id, CheckStatus.SKIP, "当前目录不是 Git 仓库。", category="git"),
            )
        lines = [line for line in result.stdout.splitlines() if line]
        findings: list[CheckFinding] = []
        if any(not line.startswith("??") for line in lines):
            findings.append(
                _finding(
                    self.rule_id, CheckStatus.WARN, "Git 工作区存在已修改文件。", category="dirty"
                )
            )
        if any(line.startswith("??") for line in lines):
            findings.append(
                _finding(
                    self.rule_id,
                    CheckStatus.WARN,
                    "Git 工作区存在未跟踪文件。",
                    category="untracked",
                )
            )
        return tuple(findings) or (
            _finding(self.rule_id, CheckStatus.PASS, "Git 工作区干净。", category="git"),
        )


DEFAULT_RULES: tuple[CheckRule, ...] = (
    ReadmeRule(),
    EnvRule(),
    PrivateKeyRule(),
    ArtifactRule(),
    LargeFileRule(),
    AbsolutePathRule(),
    HardCodedSecretRule(),
    GitStatusRule(),
)


__all__ = [
    "DEFAULT_RULES",
    "AbsolutePathRule",
    "ArtifactRule",
    "EnvRule",
    "GitStatusRule",
    "HardCodedSecretRule",
    "LargeFileRule",
    "PrivateKeyRule",
    "ReadmeRule",
]
