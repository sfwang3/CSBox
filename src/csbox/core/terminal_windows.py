from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from csbox.core.events import TerminalSize
from csbox.core.terminal import DEFAULT_TERMINAL_SIZE, TerminalBackend, TerminalBackendError


class WindowsConPTYBackend(TerminalBackend):
    """A thread-safe adapter around pywinpty's high-level PtyProcess API."""

    def __init__(self, *, pty_process_factory: Any | None = None) -> None:
        self._pty_process_factory = pty_process_factory
        self._process: Any | None = None
        self._pending: deque[bytes] = deque()
        self._exit_code: int | None = None
        self._eof = False
        self._closed = False
        self._lock = threading.RLock()

    def spawn(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        size: TerminalSize = DEFAULT_TERMINAL_SIZE,
    ) -> None:
        if not command or not all(isinstance(argument, str) and argument for argument in command):
            raise ValueError("终端启动命令不能为空，且参数必须是非空字符串。")
        with self._lock:
            if self._process is not None:
                raise RuntimeError("终端进程已经启动。")
            factory = self._load_factory()
            try:
                self._process = factory.spawn(
                    list(command),
                    cwd=None if cwd is None else str(cwd),
                    env=None if env is None else dict(env),
                    dimensions=(size.rows, size.columns),
                    backend=0,
                )
            except Exception as cause:
                raise TerminalBackendError(
                    "无法创建 Windows ConPTY，请检查 Windows 版本和 Shell 配置。", cause
                ) from cause
            self._closed = False

    def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes | None:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("单次读取字节数必须是正整数。")
        if timeout < 0:
            raise ValueError("读取超时时间不能为负数。")

        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                pending = self._take_pending(max_bytes)
                if pending is not None:
                    return pending
                if self._eof:
                    return b""
                if self._process is None:
                    raise RuntimeError("终端进程尚未启动。")
                result = self._read_process_once(max_bytes)
                if result is not None:
                    return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(0.005, remaining))

    def write(self, data: bytes) -> int:
        if not isinstance(data, bytes):
            raise TypeError("终端输入必须是 bytes。")
        text = data.decode("utf-8")
        with self._lock:
            process = self._require_process()
            if self._closed or self._eof:
                raise BrokenPipeError("终端已关闭，无法写入。")
            try:
                process.write(text)
            except Exception as cause:
                raise TerminalBackendError("写入 Windows ConPTY 失败。", cause) from cause
        return len(data)

    def resize(self, columns: int, rows: int) -> None:
        self._validate_dimensions(columns, rows)
        with self._lock:
            process = self._require_process()
            if self._closed or self._eof:
                raise RuntimeError("终端进程已经关闭。")
            try:
                process.setwinsize(rows, columns)
            except Exception as cause:
                raise TerminalBackendError("调整 Windows ConPTY 尺寸失败。", cause) from cause

    def is_alive(self) -> bool:
        with self._lock:
            if self._process is None or self._closed:
                return False
            try:
                alive = bool(self._process.isalive())
            except Exception as cause:
                raise TerminalBackendError("查询 Windows ConPTY 状态失败。", cause) from cause
            if not alive:
                self._capture_exit_status()
            return alive

    def wait(self, timeout: float = 0.0) -> int | None:
        if timeout < 0:
            raise ValueError("等待超时时间不能为负数。")
        deadline = time.monotonic() + timeout
        while True:
            if not self.is_alive():
                return self.exit_code
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(0.01, remaining))

    @property
    def exit_code(self) -> int | None:
        with self._lock:
            if self._process is not None and not self._closed:
                try:
                    if not self._process.isalive():
                        self._capture_exit_status()
                except Exception:
                    pass
            return self._exit_code

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            process = self._process
            failure: Exception | None = None
            if process is not None:
                self._drain_available()
                self._capture_exit_status()
                try:
                    process.close()
                except Exception as cause:
                    failure = cause
                self._capture_exit_status()
            self._closed = True
            self._eof = True
            if failure is not None:
                raise TerminalBackendError("关闭 Windows ConPTY 失败。", failure) from failure

    def _load_factory(self) -> Any:
        if self._pty_process_factory is not None:
            return self._pty_process_factory
        try:
            from winpty import PtyProcess
        except (ImportError, OSError) as cause:
            raise TerminalBackendError(
                "未安装 pywinpty，无法使用 Windows ConPTY。请在 Windows 上运行 uv sync。",
                cause,
            ) from cause
        self._pty_process_factory = PtyProcess
        return PtyProcess

    def _read_process_once(self, max_bytes: int) -> bytes | None:
        try:
            text = self._process.read(max_bytes, blocking=False)
        except EOFError:
            self._capture_exit_status()
            self._eof = True
            return b""
        except Exception as cause:
            raise TerminalBackendError("读取 Windows ConPTY 输出失败。", cause) from cause
        if text:
            if not isinstance(text, str):
                cause = TypeError(f"PtyProcess.read returned {type(text).__name__}")
                raise TerminalBackendError(
                    "Windows ConPTY 返回了无效的文本数据。", cause
                ) from cause
            encoded = text.encode("utf-8")
            if len(encoded) > max_bytes:
                self._pending.append(encoded[max_bytes:])
                return encoded[:max_bytes]
            return encoded
        try:
            alive = bool(self._process.isalive())
        except Exception as cause:
            raise TerminalBackendError("查询 Windows ConPTY 状态失败。", cause) from cause
        if alive:
            return None
        self._capture_exit_status()
        self._eof = True
        return b""

    def _drain_available(self) -> None:
        process = self._process
        if process is None:
            return
        for _ in range(64):
            try:
                text = process.read(65536, blocking=False)
            except EOFError:
                break
            except Exception:
                break
            if not text:
                break
            if isinstance(text, str):
                self._pending.append(text.encode("utf-8"))

    def _take_pending(self, max_bytes: int) -> bytes | None:
        if not self._pending:
            return None
        frame = self._pending.popleft()
        if len(frame) <= max_bytes:
            return frame
        self._pending.appendleft(frame[max_bytes:])
        return frame[:max_bytes]

    def _capture_exit_status(self) -> None:
        if self._process is None or self._exit_code is not None:
            return
        try:
            status = self._process.exitstatus
            if callable(status):
                status = status()
        except Exception:
            return
        if status is not None:
            self._exit_code = int(status)

    def _require_process(self) -> Any:
        if self._process is None:
            raise RuntimeError("终端进程尚未启动。")
        return self._process

    @staticmethod
    def _validate_dimensions(columns: int, rows: int) -> None:
        for name, value in (("columns", columns), ("rows", rows)):
            if type(value) is not int:
                raise TypeError(f"终端尺寸 {name} 必须是整数。")
            if value <= 0:
                raise ValueError(f"终端尺寸 {name} 必须是正数。")
