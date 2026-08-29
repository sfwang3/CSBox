from __future__ import annotations

import codecs
import os
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from csbox.core.events import TerminalSize
from csbox.core.terminal import (
    DEFAULT_TERMINAL_SIZE,
    TerminalBackend,
    TerminalBackendError,
    TerminalProcessExited,
)


def _classify_spawn_failure(cause: BaseException) -> tuple[str, str, int | None]:
    error_code = _windows_error_code(cause)
    if error_code in {193, 216}:
        return (
            "shell_executable_unlaunchable",
            "Shell 可执行文件无效或无法启动。请重新安装 Shell 后重试。",
            error_code,
        )
    if isinstance(cause, FileNotFoundError) or error_code in {2, 3}:
        return (
            "shell_executable_unavailable",
            "未找到 Shell 可执行文件。请检查 Shell 安装后重试。",
            error_code,
        )
    return (
        "conpty_initialization_failure",
        "无法初始化 Windows ConPTY。请确认 Windows 终端能力后重试。",
        error_code,
    )


def _windows_error_code(cause: BaseException) -> int | None:
    error_code = getattr(cause, "winerror", None)
    if error_code is None:
        error_code = getattr(cause, "errno", None)
    if isinstance(error_code, int):
        return error_code
    if os.name != "nt":
        return None
    try:
        import ctypes

        actual = _normalized_windows_error(str(cause))
        for candidate in (2, 3, 193, 216):
            expected = _normalized_windows_error(ctypes.FormatError(candidate))
            if actual and expected and expected in actual:
                return candidate
    except (AttributeError, OSError, ValueError):
        return None
    return None


def _normalized_windows_error(message: str) -> str:
    return " ".join(message.casefold().strip().rstrip(".").split())


class WindowsConPTYBackend(TerminalBackend):
    """A thread-safe adapter around pywinpty's high-level PtyProcess API."""

    _READ_SIZE = 65536
    _CLOSE_GRACE = 0.05
    _EOF_EXIT_GRACE = 0.5
    _READER_JOIN_TIMEOUT = 0.5

    def __init__(
        self,
        *,
        pty_process_factory: Any | None = None,
        write_timeout: float = 1.0,
        max_output_buffer_bytes: int = 1024 * 1024,
    ) -> None:
        if write_timeout <= 0:
            raise ValueError("终端写入超时时间必须是正数。")
        if type(max_output_buffer_bytes) is not int or max_output_buffer_bytes <= 0:
            raise ValueError("终端输出缓冲区上限必须是正整数。")
        self._pty_process_factory = pty_process_factory
        self._write_timeout = write_timeout
        self._max_output_buffer_bytes = max_output_buffer_bytes
        self._process: Any | None = None
        self._reader_thread: threading.Thread | None = None
        self._output: deque[bytes] = deque()
        self._output_head_offset = 0
        self._buffered_output_bytes = 0
        self._reader_done = False
        self._reader_error: Exception | None = None
        self._input_buffer = b""
        self._pywinpty_async_write = False
        self._exit_code: int | None = None
        self._closed = False
        self._closing = False
        self._close_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._output_ready = threading.Condition()

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
        with self._state_lock:
            if self._process is not None:
                raise RuntimeError("终端进程已经启动。")
            factory = self._load_factory()
            self._pywinpty_async_write = getattr(factory, "__module__", "") == "winpty.ptyprocess"
            child_env = os.environ.copy()
            if env is not None:
                child_env.update(env)
            try:
                process = factory.spawn(
                    list(command),
                    cwd=None if cwd is None else str(cwd),
                    env=child_env,
                    dimensions=(size.rows, size.columns),
                    backend="0",
                )
            except Exception as cause:
                kind, message, native_code = _classify_spawn_failure(cause)
                debug_context = [
                    f"resolved_executable={command[0]}",
                    f"cwd={cwd}" if cwd is not None else "cwd=<inherited>",
                ]
                if native_code is not None:
                    debug_context.append(f"native_code={native_code}")
                raise TerminalBackendError(
                    message,
                    cause,
                    kind=kind,
                    debug_context=tuple(debug_context),
                ) from cause
            self._process = process
            self._closed = False
            reader = threading.Thread(
                target=self._reader_loop,
                args=(process,),
                name="csbox-conpty-reader",
                daemon=True,
            )
            self._reader_thread = reader
            reader.start()

    def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes | None:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("单次读取字节数必须是正整数。")
        if timeout < 0:
            raise ValueError("读取超时时间不能为负数。")
        if self._process is None:
            raise RuntimeError("终端进程尚未启动。")

        deadline = time.monotonic() + timeout
        with self._output_ready:
            while True:
                if self._output:
                    data = self._drain_output(max_bytes)
                    self._output_ready.notify_all()
                    return data
                if self._reader_error is not None:
                    cause = self._reader_error
                    raise TerminalBackendError("读取 Windows ConPTY 输出失败。", cause) from cause
                if self._reader_done:
                    return b""
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._output_ready.wait(remaining)

    def write(self, data: bytes) -> int:
        if not isinstance(data, bytes):
            raise TypeError("终端输入必须是 bytes。")
        with self._state_lock:
            process = self._require_process()
            if self._closed:
                raise BrokenPipeError("终端已关闭，无法写入。")
            candidate = self._input_buffer + data
            self._complete_utf8_length(candidate)
            self._input_buffer = candidate
            self._flush_complete_input(process)
        return len(data)

    def resize(self, columns: int, rows: int) -> None:
        self._validate_dimensions(columns, rows)
        with self._state_lock:
            process = self._require_process()
            if self._closed:
                raise RuntimeError("终端进程已经关闭。")
            try:
                process.setwinsize(rows, columns)
            except Exception as cause:
                raise TerminalBackendError("调整 Windows ConPTY 尺寸失败。", cause) from cause

    def is_alive(self) -> bool:
        with self._state_lock:
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
        with self._state_lock:
            self._capture_exit_status()
            return self._exit_code

    def close(self) -> None:
        with self._close_lock:
            with self._state_lock:
                if self._closed:
                    return
                process = self._process
                if process is None:
                    self._closed = True
                    with self._output_ready:
                        self._reader_done = True
                        self._output_ready.notify_all()
                    return

            force = self._wait_for_natural_exit(process, self._CLOSE_GRACE)
            reader = self._reader_thread
            if not force and reader is not None:
                reader.join(self._READER_JOIN_TIMEOUT)

            with self._state_lock:
                self._closing = True
                with self._output_ready:
                    self._output_ready.notify_all()
                try:
                    process.close(force=force)
                except Exception as cause:
                    if not force and self._is_expected_closed_handle(cause):
                        pass
                    else:
                        self._closing = False
                        raise TerminalBackendError(
                            "关闭 Windows ConPTY 失败，可重试清理。", cause
                        ) from cause

            if reader is not None:
                reader.join(self._READER_JOIN_TIMEOUT)
                if reader.is_alive():
                    with self._state_lock:
                        self._closing = False
                    cause = TimeoutError("pywinpty reader did not stop after close")
                    raise TerminalBackendError(
                        "Windows ConPTY 读取线程未能及时停止，可重试清理。", cause
                    ) from cause

            with self._state_lock:
                self._capture_exit_status()
                self._closed = True
                self._closing = False

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

    def _reader_loop(self, process: Any) -> None:
        reader_error: Exception | None = None
        try:
            while True:
                try:
                    text = process.read(self._READ_SIZE)
                except EOFError as cause:
                    if self._confirm_eof_after_process_exit(process):
                        break
                    raise RuntimeError(
                        "Windows ConPTY output closed while the child process is still alive"
                    ) from cause
                if not isinstance(text, str):
                    raise TypeError(f"PtyProcess.read returned {type(text).__name__}")
                if text == "":
                    # pywinpty uses an empty string for its internal
                    # ``0011Ignore`` no-output sentinel.  On some native
                    # PowerShell 5.1 exits the sentinel is the final read,
                    # so pair it with child liveness before treating it as
                    # no output.  Actual EOF is also reported as EOFError
                    # by PtyProcess.read().
                    if not bool(process.isalive()):
                        self._capture_exit_status()
                        break
                    continue
                encoded = text.encode("utf-8")
                offset = 0
                while offset < len(encoded):
                    with self._output_ready:
                        while (
                            self._buffered_output_bytes >= self._max_output_buffer_bytes
                            and not self._closing
                        ):
                            self._output_ready.wait()
                        if self._closing:
                            return
                        available = self._max_output_buffer_bytes - self._buffered_output_bytes
                        chunk = encoded[offset : offset + available]
                        self._output.append(chunk)
                        self._buffered_output_bytes += len(chunk)
                        offset += len(chunk)
                        self._output_ready.notify_all()
        except EOFError:
            pass
        except Exception as cause:
            if not self._closing and not self._is_expected_reader_close(process, cause):
                reader_error = cause
        finally:
            with self._output_ready:
                self._reader_error = reader_error
                self._reader_done = True
                self._output_ready.notify_all()

    def _confirm_eof_after_process_exit(self, process: Any) -> bool:
        deadline = time.monotonic() + self._EOF_EXIT_GRACE
        while True:
            try:
                if not bool(process.isalive()):
                    return True
            except Exception as cause:
                raise RuntimeError(
                    "Windows ConPTY could not confirm child exit after output EOF"
                ) from cause
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    def _is_expected_reader_close(self, process: Any, cause: BaseException) -> bool:
        """Recognize a native output handle closing after child exit.

        ConPTY consumers observe EOF through either pywinpty's ``EOFError`` or
        a Windows broken/closed-pipe error depending on which native layer
        wins the race.  Only the latter codes, with a dead child, are normal;
        arbitrary reader exceptions remain backend failures.
        """

        if not isinstance(cause, OSError):
            return False
        error_code = getattr(cause, "winerror", None)
        if error_code is None:
            error_code = getattr(cause, "errno", None)
        if error_code not in {6, 109, 232}:
            return False
        try:
            return not bool(process.isalive())
        except Exception:
            return self._exit_code is not None

    def _drain_output(self, max_bytes: int) -> bytes:
        remaining = min(max_bytes, self._buffered_output_bytes)
        parts: list[bytes] = []
        while remaining and self._output:
            head = self._output[0]
            available = len(head) - self._output_head_offset
            take = min(remaining, available)
            start = self._output_head_offset
            parts.append(head[start : start + take])
            self._output_head_offset += take
            self._buffered_output_bytes -= take
            remaining -= take
            if self._output_head_offset == len(head):
                self._output.popleft()
                self._output_head_offset = 0
        return b"".join(parts)

    def _flush_complete_input(self, process: Any) -> None:
        if self._pywinpty_async_write:
            self._flush_pywinpty_input(process)
            return

        deadline = time.monotonic() + self._write_timeout
        while True:
            complete_length = self._complete_utf8_length(self._input_buffer)
            if complete_length == 0:
                return
            complete_bytes = self._input_buffer[:complete_length]
            text = complete_bytes.decode("utf-8")
            try:
                written = process.write(text)
            except Exception as cause:
                self._raise_write_failure(process, cause)
            if type(written) is not int or written < 0 or written > len(complete_bytes):
                cause = ValueError(f"invalid PtyProcess.write result: {written!r}")
                raise TerminalBackendError(
                    "Windows ConPTY 返回了无效的写入字节数。", cause
                ) from cause
            if written == 0:
                if time.monotonic() >= deadline:
                    cause = BlockingIOError("PtyProcess.write made no progress")
                    raise TerminalBackendError(
                        "Windows ConPTY 写入暂时无法取得进展。", cause
                    ) from cause
                time.sleep(0.001)
                continue
            try:
                complete_bytes[:written].decode("utf-8")
                complete_bytes[written:].decode("utf-8")
            except UnicodeDecodeError as cause:
                raise TerminalBackendError(
                    "Windows ConPTY 在 UTF-8 字符中间报告了部分写入，无法安全续写。", cause
                ) from cause
            self._input_buffer = self._input_buffer[written:]

    def _flush_pywinpty_input(self, process: Any) -> None:
        """Submit complete UTF-8 input without misreading pywinpty's async count.

        pywinpty's ConPTY implementation uses an overlapped input pipe. Its
        high-level ``write`` returns the number of bytes completed from a
        previous call, so the first successful submission commonly returns
        zero even though the current input is already queued. The Rust layer
        submits every chunk or raises; consuming the current buffer after a
        successful call avoids resending it.
        """
        while True:
            complete_length = self._complete_utf8_length(self._input_buffer)
            if complete_length == 0:
                return
            complete_bytes = self._input_buffer[:complete_length]
            text = complete_bytes.decode("utf-8")
            try:
                written = process.write(text)
            except Exception as cause:
                self._raise_write_failure(process, cause)
            if type(written) is not int or written < 0:
                cause = ValueError(f"invalid PtyProcess.write result: {written!r}")
                raise TerminalBackendError(
                    "Windows ConPTY 返回了无效的写入字节数。", cause
                ) from cause
            self._input_buffer = self._input_buffer[complete_length:]

    def _raise_write_failure(self, process: Any, cause: Exception) -> None:
        if self._is_expected_input_close(process, cause):
            self._capture_exit_status()
            self._input_buffer = b""
            raise TerminalProcessExited(self._exit_code) from cause
        raise TerminalBackendError("写入 Windows ConPTY 失败。", cause) from cause

    def _is_expected_input_close(self, process: Any, cause: BaseException) -> bool:
        if not isinstance(cause, EOFError) and not self._is_expected_closed_handle(cause):
            return False
        try:
            if bool(process.isalive()):
                return False
        except Exception:
            self._capture_exit_status()
            return self._exit_code is not None
        self._capture_exit_status()
        return True

    @staticmethod
    def _complete_utf8_length(data: bytes) -> int:
        decoder = codecs.getincrementaldecoder("utf-8")()
        decoder.decode(data, final=False)
        pending, _ = decoder.getstate()
        return len(data) - len(pending)

    def _wait_for_natural_exit(self, process: Any, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            try:
                alive = bool(process.isalive())
            except Exception as cause:
                if self._reader_done and self._is_expected_closed_handle(cause):
                    self._capture_exit_status()
                    return False
                raise TerminalBackendError("查询 Windows ConPTY 状态失败。", cause) from cause
            if not alive:
                self._capture_exit_status()
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.01, remaining))

    def _is_expected_closed_handle(self, cause: BaseException) -> bool:
        if isinstance(cause, OSError):
            error_code = getattr(cause, "winerror", None)
            if error_code is None:
                error_code = getattr(cause, "errno", None)
            return error_code in {6, 109, 232}
        if isinstance(cause, ValueError):
            message = str(cause).casefold()
            return any(
                marker in message for marker in ("closed", "invalid handle", "bad file descriptor")
            )
        return False

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
