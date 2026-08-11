from __future__ import annotations

import pytest

from csbox.lab.keymap import CaptureBindingProbe, CaptureKeyMatcher

F12 = b"\x1b[24~"


def collect(matcher: CaptureKeyMatcher, chunks: list[bytes]) -> tuple[bytes, int]:
    forwarded = bytearray()
    captures = 0
    for chunk in chunks:
        result = matcher.feed(chunk)
        forwarded.extend(result.forwarded)
        captures += result.captures
    flushed = matcher.flush()
    forwarded.extend(flushed.forwarded)
    captures += flushed.captures
    return bytes(forwarded), captures


def test_f12_is_filtered_and_emits_capture_event() -> None:
    forwarded, captures = collect(CaptureKeyMatcher("f12"), [b"A", F12, b"B"])

    assert forwarded == b"AB"
    assert captures == 1


@pytest.mark.parametrize("split", range(1, len(F12)))
def test_f12_prefix_split_at_every_boundary(split: int) -> None:
    forwarded, captures = collect(CaptureKeyMatcher("f12"), [F12[:split], F12[split:]])

    assert forwarded == b""
    assert captures == 1


def test_false_prefix_is_flushed_in_original_order() -> None:
    forwarded, captures = collect(CaptureKeyMatcher("f12"), [b"x\x1b[2Xy"])

    assert forwarded == b"x\x1b[2Xy"
    assert captures == 0


def test_multiple_captures_and_ordinary_controls_are_preserved() -> None:
    forwarded, captures = collect(CaptureKeyMatcher("f12"), [b"\x03", F12 + b"a" + F12 + b"\r"])

    assert forwarded == b"\x03a\r"
    assert captures == 2


def test_flush_forwards_an_unfinished_non_match() -> None:
    matcher = CaptureKeyMatcher("f12")

    assert matcher.feed(b"\x1b[").forwarded == b""
    assert matcher.flush().forwarded == b"\x1b["
    assert matcher.feed(b"x").forwarded == b"x"


def test_custom_binding_is_supported_without_assuming_f12() -> None:
    matcher = CaptureKeyMatcher("ctrl-space")

    result = matcher.feed(b"a\x00b")

    assert result.forwarded == b"ab"
    assert result.captures == 1


def test_binding_probe_reports_host_advisory_without_mutating_environment() -> None:
    environment = {"TERM_PROGRAM": "vscode", "TERM": "xterm-256color"}
    advisory = CaptureBindingProbe("f12", environ=environment, input_supported=True).probe()

    assert advisory.key == "f12"
    assert advisory.supported is True
    assert "VS Code" in advisory.message
    assert environment == {"TERM_PROGRAM": "vscode", "TERM": "xterm-256color"}


def test_binding_probe_marks_unavailable_input_adapter() -> None:
    advisory = CaptureBindingProbe("f12", input_supported=False).probe()

    assert advisory.supported is False
    assert "降级" in advisory.message or "capture" in advisory.message.lower()
