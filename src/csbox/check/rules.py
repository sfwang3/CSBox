from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from csbox.check.detectors import PRUNED_DIRECTORIES
from csbox.check.models import CheckContext, CheckFinding, CheckStatus
from csbox.core.subprocess_env import minimal_subprocess_environment

_MAX_GIT_STATUS_BYTES = 4 * 1024 * 1024


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
        markers = tuple(marker.decode("ascii") for marker in PRIVATE_KEY_MARKERS)
        for entry in context.inventory.files:
            has_marker = context.inventory.contains_markers(entry, markers)
            if has_marker:
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
_WINDOWS_ABSOLUTE_PATH_BYTES = re.compile(rb"(?<![A-Za-z0-9_])[A-Za-z]:\\")
_UNIX_ABSOLUTE_PATH_BYTES = re.compile(
    rb"(?<![A-Za-z0-9_])/(?:home|Users|tmp|mnt|workspace|opt|var)/"
)
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?key|token|secret|password|passwd)\b"
    r"[^\S\r\n]*[:=][^\S\r\n]*['\"][^'\"\r\n]{8}"
)
_SECRET_ASSIGNMENT_BYTES = re.compile(
    rb"(?i)\b(?:api[_-]?key|access[_-]?key|token|secret|password|passwd)\b"
    rb"[^\S\r\n]*[:=][^\S\r\n]*['\"][^'\"\r\n]{8}"
)
_STREAM_SCAN_CHUNK_BYTES = 1024 * 1024
_STREAM_SCAN_OVERLAP_BYTES = 512
_HORIZONTAL_WHITESPACE_BYTES = frozenset(b" \t\v\f")


def _stream_pattern_lines(
    context: CheckContext,
    entry,
    patterns: tuple[re.Pattern[bytes], ...],
    *,
    collapse_horizontal_whitespace: bool = False,
) -> tuple[tuple[int, int], ...]:
    """Return pattern indexes and line numbers using bounded-memory reads."""
    matches: set[tuple[int, int]] = set()
    overlap = b""
    newlines_before_overlap = 0
    in_horizontal_whitespace = False
    try:
        with context.inventory.open_entry(entry) as stream:
            while chunk := stream.read(_STREAM_SCAN_CHUNK_BYTES):
                if collapse_horizontal_whitespace:
                    normalized = bytearray()
                    for value in chunk:
                        if value in _HORIZONTAL_WHITESPACE_BYTES:
                            if not in_horizontal_whitespace:
                                normalized.append(ord(" "))
                            in_horizontal_whitespace = True
                        else:
                            normalized.append(value)
                            in_horizontal_whitespace = False
                    chunk = bytes(normalized)
                data = overlap + chunk
                for pattern_index, pattern in enumerate(patterns):
                    line = 1 + newlines_before_overlap
                    cursor = 0
                    for match in pattern.finditer(data):
                        line += data[cursor : match.start()].count(b"\n")
                        cursor = match.start()
                        matches.add((pattern_index, line))
                retained = min(len(data), _STREAM_SCAN_OVERLAP_BYTES)
                discarded = data[:-retained] if retained else data
                newlines_before_overlap += discarded.count(b"\n")
                overlap = data[-retained:] if retained else b""
    except (OSError, ValueError):
        return ()
    return tuple(sorted(matches, key=lambda item: (item[1], item[0])))


class AbsolutePathRule:
    rule_id = "local-absolute-path"

    def evaluate(self, context: CheckContext) -> tuple[CheckFinding, ...]:
        findings: list[CheckFinding] = []
        for entry in context.inventory.files:
            content = context.inventory.text(entry)
            if content is not None:
                matches = []
                for line_number, line in enumerate(content.splitlines(), start=1):
                    if WINDOWS_ABSOLUTE_PATH.search(line):
                        matches.append((0, line_number))
                    if UNIX_ABSOLUTE_PATH.search(line):
                        matches.append((1, line_number))
            elif entry.size > context.inventory.scan_limit_bytes:
                matches = list(
                    _stream_pattern_lines(
                        context,
                        entry,
                        (_WINDOWS_ABSOLUTE_PATH_BYTES, _UNIX_ABSOLUTE_PATH_BYTES),
                    )
                )
            else:
                matches = []
            for pattern_index, line_number in matches:
                if pattern_index == 0:
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
                else:
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
        for entry in context.inventory.files:
            content = context.inventory.text(entry)
            if content is not None:
                lines = (
                    line_number
                    for line_number, line in enumerate(content.splitlines(), start=1)
                    if SECRET_ASSIGNMENT.search(line)
                )
            else:
                lines = (
                    line_number
                    for _, line_number in _stream_pattern_lines(
                        context,
                        entry,
                        (_SECRET_ASSIGNMENT_BYTES,),
                        collapse_horizontal_whitespace=True,
                    )
                )
            for line_number in lines:
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
            with tempfile.TemporaryFile() as status_output:
                result = subprocess.run(
                    [
                        "git",
                        "-c",
                        "core.fsmonitor=false",
                        "-C",
                        str(context.root),
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=all",
                    ],
                    stdout=status_output,
                    stderr=subprocess.DEVNULL,
                    env=minimal_subprocess_environment(),
                    timeout=2.0,
                    check=False,
                    shell=False,
                )
                status_output.seek(0)
                raw_status = status_output.read(_MAX_GIT_STATUS_BYTES + 1)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return (
                _finding(self.rule_id, CheckStatus.SKIP, "无法读取 Git 状态。", category="git"),
            )
        if result.returncode != 0:
            return (
                _finding(self.rule_id, CheckStatus.SKIP, "当前目录不是 Git 仓库。", category="git"),
            )
        if len(raw_status) > _MAX_GIT_STATUS_BYTES:
            return (
                _finding(
                    self.rule_id,
                    CheckStatus.WARN,
                    "Git 状态输出过大，无法可靠判断工作区状态。",
                    category="git",
                ),
            )
        lines = [line for line in raw_status.decode("utf-8", errors="replace").splitlines() if line]
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


def run_deep_secret_scan(root: Path) -> CheckFinding:
    """Run gitleaks without retaining or displaying its output."""
    tool = shutil.which("gitleaks")
    if not tool:
        return _finding(
            "deep-secret-scan",
            CheckStatus.SKIP,
            "未找到 gitleaks，未执行深度 secret 扫描；请先安装 gitleaks。",
            category="deep-secret-scan",
        )

    resolved_root = Path(root).resolve()
    command = [
        tool,
        "detect",
        "--source",
        str(resolved_root),
        "--no-banner",
        "--redact",
        "--exit-code",
        "1",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=resolved_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=minimal_subprocess_environment(),
            timeout=60.0,
            check=False,
            shell=False,
        )
    except FileNotFoundError:
        return _finding(
            "deep-secret-scan",
            CheckStatus.SKIP,
            "未找到 gitleaks，未执行深度 secret 扫描；请先安装 gitleaks。",
            category="deep-secret-scan",
        )
    except (OSError, subprocess.TimeoutExpired):
        return _finding(
            "deep-secret-scan",
            CheckStatus.WARN,
            "gitleaks 深度扫描未完成，工具无法可靠执行。",
            category="deep-secret-scan",
        )

    if result.returncode == 0:
        return _finding(
            "deep-secret-scan",
            CheckStatus.PASS,
            "gitleaks 深度扫描未发现 secret。",
            category="deep-secret-scan",
        )
    if result.returncode == 1:
        return _finding(
            "deep-secret-scan",
            CheckStatus.FAIL,
            "gitleaks 深度扫描发现疑似 secret；请根据工具报告清理后重试。",
            category="deep-secret-scan",
        )
    return _finding(
        "deep-secret-scan",
        CheckStatus.WARN,
        "gitleaks 深度扫描未完成，工具返回错误。",
        category="deep-secret-scan",
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
    "run_deep_secret_scan",
]
