from __future__ import annotations

import errno
import json
import os
import selectors
import signal
import struct
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path

from csbox.core.events import TerminalSize
from csbox.core.terminal import DEFAULT_TERMINAL_SIZE, TerminalBackend, TerminalBackendError


class UnixPTYBackend(TerminalBackend):
    """A real Unix pseudoterminal backed by a controlling terminal."""

    _SPAWN_HANDSHAKE_TIMEOUT = 5.0
    _WRITE_TIMEOUT = 0.25

    def __init__(self) -> None:
        self._pid: int | None = None
        self._master_fd: int | None = None
        self._selector: selectors.BaseSelector | None = None
        self._exit_code: int | None = None
        self._reaped = False
        self._eof = False
        self._closed = False

    def spawn(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        size: TerminalSize = DEFAULT_TERMINAL_SIZE,
    ) -> None:
        import fcntl
        import pty
        import termios

        if self._pid is not None:
            raise RuntimeError("终端进程已经启动。")
        if not command or not all(isinstance(argument, str) and argument for argument in command):
            raise ValueError("终端启动命令不能为空，且参数必须是非空字符串。")

        child_env = os.environ.copy()
        if env is not None:
            child_env.update(env)
        child_env.setdefault("TERM", "xterm-256color")
        error_read_fd, error_write_fd = self._create_cloexec_pipe()
        try:
            pid, master_fd = pty.fork()
        except OSError as cause:
            os.close(error_read_fd)
            os.close(error_write_fd)
            raise TerminalBackendError("无法创建 Unix PTY，请检查系统终端能力。", cause) from cause

        if pid == 0:
            os.close(error_read_fd)
            try:
                window_size = struct.pack("HHHH", size.rows, size.columns, 0, 0)
                fcntl.ioctl(0, termios.TIOCSWINSZ, window_size)
                if cwd is not None:
                    os.chdir(cwd)
                os.execvpe(command[0], list(command), child_env)
            except BaseException as cause:
                try:
                    self._send_child_error(error_write_fd, cause)
                finally:
                    os._exit(127)

        os.close(error_write_fd)
        child_error = self._receive_child_error(error_read_fd, pid, master_fd)
        if child_error is not None:
            raise TerminalBackendError(
                "无法启动 Unix PTY 子进程，请检查命令和工作目录。", child_error
            ) from child_error

        self._pid = pid
        self._master_fd = master_fd
        self._selector = selectors.DefaultSelector()
        self._selector.register(master_fd, selectors.EVENT_READ)
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self._closed = False
        try:
            self.resize(size.columns, size.rows)
        except BaseException:
            self.close()
            raise

    def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes | None:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("单次读取字节数必须是正整数。")
        if timeout < 0:
            raise ValueError("读取超时时间不能为负数。")
        if self._eof:
            self._reap_nonblocking()
            return b""
        if self._master_fd is None or self._selector is None:
            raise RuntimeError("终端进程尚未启动。")

        events = self._selector.select(timeout)
        if not events:
            self._reap_nonblocking()
            return None
        try:
            data = os.read(self._master_fd, max_bytes)
        except BlockingIOError:
            return None
        except OSError as cause:
            if cause.errno in (errno.EIO, errno.EBADF):
                self._mark_eof()
                self._reap_nonblocking()
                return b""
            raise TerminalBackendError("读取 Unix PTY 输出失败。", cause) from cause
        if data:
            return data
        self._mark_eof()
        self._reap_nonblocking()
        return b""

    def write(self, data: bytes) -> int:
        if not isinstance(data, bytes):
            raise TypeError("终端输入必须是 bytes。")
        if self._master_fd is None or self._eof:
            raise BrokenPipeError("终端已关闭，无法写入。")
        if not data:
            return 0
        deadline = time.monotonic() + self._WRITE_TIMEOUT
        while True:
            try:
                return os.write(self._master_fd, data)
            except InterruptedError:
                continue
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._wait_until_writable(remaining):
                    return 0
            except OSError as cause:
                if cause.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not self._wait_until_writable(remaining):
                        return 0
                    continue
                if cause.errno in (errno.EIO, errno.EBADF):
                    raise BrokenPipeError("终端已关闭，无法写入。") from cause
                raise TerminalBackendError("写入 Unix PTY 失败。", cause) from cause

    def resize(self, columns: int, rows: int) -> None:
        import fcntl
        import termios

        self._validate_dimensions(columns, rows)
        if self._master_fd is None or self._eof:
            raise RuntimeError("终端进程尚未启动或已经关闭。")
        window_size = struct.pack("HHHH", rows, columns, 0, 0)
        try:
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, window_size)
        except OSError as cause:
            raise TerminalBackendError("调整 Unix PTY 尺寸失败。", cause) from cause

    def is_alive(self) -> bool:
        self._reap_nonblocking()
        return self._pid is not None and not self._reaped

    def wait(self, timeout: float = 0.0) -> int | None:
        if timeout < 0:
            raise ValueError("等待超时时间不能为负数。")
        if self._pid is None:
            return self._exit_code
        deadline = time.monotonic() + timeout
        while True:
            self._reap_nonblocking()
            if self._reaped:
                return self._exit_code
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(0.01, remaining))

    @property
    def exit_code(self) -> int | None:
        self._reap_nonblocking()
        return self._exit_code

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._reap_nonblocking()
        self._close_master()
        if self.is_alive():
            self._signal_process_group(signal.SIGHUP)
            self.wait(timeout=0.2)
        if self.is_alive():
            self._signal_process_group(signal.SIGTERM)
            self.wait(timeout=0.3)
        if self.is_alive():
            self._signal_process_group(signal.SIGKILL)
            self.wait(timeout=0.5)

    @staticmethod
    def _validate_dimensions(columns: int, rows: int) -> None:
        for name, value in (("columns", columns), ("rows", rows)):
            if type(value) is not int:
                raise TypeError(f"终端尺寸 {name} 必须是整数。")
            if value <= 0:
                raise ValueError(f"终端尺寸 {name} 必须是正数。")

    def _mark_eof(self) -> None:
        self._eof = True
        self._close_master()

    def _close_master(self) -> None:
        master_fd = self._master_fd
        selector = self._selector
        self._master_fd = None
        self._selector = None
        if selector is not None:
            if master_fd is not None:
                with suppress(KeyError, ValueError):
                    selector.unregister(master_fd)
            selector.close()
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError as cause:
                if cause.errno != errno.EBADF:
                    raise

    def _reap_nonblocking(self) -> None:
        if self._pid is None or self._reaped:
            return
        try:
            waited_pid, status = os.waitpid(self._pid, os.WNOHANG)
        except ChildProcessError:
            self._reaped = True
            return
        if waited_pid == self._pid:
            self._exit_code = os.waitstatus_to_exitcode(status)
            self._reaped = True

    def _signal_process_group(self, sig: signal.Signals) -> None:
        if self._pid is None or self._reaped:
            return
        try:
            os.killpg(self._pid, sig)
        except ProcessLookupError:
            self._reap_nonblocking()

    @staticmethod
    def _create_cloexec_pipe() -> tuple[int, int]:
        if hasattr(os, "pipe2"):
            return os.pipe2(os.O_CLOEXEC)
        read_fd, write_fd = os.pipe()
        os.set_inheritable(read_fd, False)
        os.set_inheritable(write_fd, False)
        return read_fd, write_fd

    @staticmethod
    def _send_child_error(write_fd: int, cause: BaseException) -> None:
        filename = getattr(cause, "filename", None)
        payload = json.dumps(
            {
                "errno": getattr(cause, "errno", None),
                "filename": None if filename is None else os.fspath(filename),
                "message": str(cause),
                "type": type(cause).__name__,
            },
            ensure_ascii=False,
        ).encode("utf-8", errors="replace")
        view = memoryview(payload)
        while view:
            written = os.write(write_fd, view)
            view = view[written:]

    def _receive_child_error(
        self,
        read_fd: int,
        pid: int,
        master_fd: int,
    ) -> Exception | None:
        selector = selectors.DefaultSelector()
        try:
            selector.register(read_fd, selectors.EVENT_READ)
            if not selector.select(self._SPAWN_HANDSHAKE_TIMEOUT):
                cause: Exception = TimeoutError("Unix PTY child setup handshake timed out")
                self._abort_failed_spawn(pid, master_fd)
                return cause
            chunks = bytearray()
            while True:
                chunk = os.read(read_fd, 4096)
                if not chunk:
                    break
                chunks.extend(chunk)
        finally:
            selector.close()
            os.close(read_fd)
        if not chunks:
            return None
        self._close_failed_master(master_fd)
        self._reap_failed_child(pid)
        try:
            details = json.loads(chunks.decode("utf-8"))
            error_number = details.get("errno")
            message = details.get("message") or details.get("type") or "child setup failed"
            filename = details.get("filename")
            if error_number is not None:
                return OSError(error_number, message, filename)
            return RuntimeError(message)
        except (TypeError, ValueError, UnicodeError) as cause:
            return RuntimeError(f"invalid child setup error payload: {cause}")

    def _abort_failed_spawn(self, pid: int, master_fd: int) -> None:
        self._close_failed_master(master_fd)
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        self._reap_failed_child(pid)

    @staticmethod
    def _close_failed_master(master_fd: int) -> None:
        with suppress(OSError):
            os.close(master_fd)

    @staticmethod
    def _reap_failed_child(pid: int) -> None:
        while True:
            try:
                os.waitpid(pid, 0)
                return
            except InterruptedError:
                continue
            except ChildProcessError:
                return

    def _wait_until_writable(self, timeout: float) -> bool:
        if self._master_fd is None:
            return False
        selector = selectors.DefaultSelector()
        try:
            selector.register(self._master_fd, selectors.EVENT_WRITE)
            return bool(selector.select(timeout))
        except OSError as cause:
            if cause.errno in (errno.EIO, errno.EBADF):
                raise BrokenPipeError("终端已关闭，无法写入。") from cause
            raise TerminalBackendError("等待 Unix PTY 可写状态失败。", cause) from cause
        finally:
            selector.close()
