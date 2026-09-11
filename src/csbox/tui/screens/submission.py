"""Beginner-first Submission handoff workflow."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Static

from csbox.core.display_width import truncate_cells
from csbox.core.text_layout import wrap_cells
from csbox.evidence.repository import EvidenceSetRepository
from csbox.locales import Translator
from csbox.submission.models import SubmissionReadiness
from csbox.submission.verifier import SubmissionVerifier

SubmissionServiceFactory = Callable[[], object]
ReportProfileScreenFactory = Callable[[str], Screen[None]]


class SubmissionScreen(Screen[None]):
    """Select an Evidence Set, inspect preflight, and prepare one handoff."""

    BINDINGS = (
        Binding("up", "previous", "上一项", show=False, priority=True),
        Binding("down", "next", "下一项", show=False, priority=True),
        Binding("enter", "activate", "确认", show=False, priority=True),
        Binding("escape", "back", "返回", show=False, priority=True),
        Binding("q", "back", "返回", show=False, priority=True),
        Binding("v", "verify", "校验", show=False, priority=True),
    )

    def __init__(
        self,
        *,
        repository: object | None,
        locale: Translator,
        service: object,
        verifier_factory: Callable[[], object] = SubmissionVerifier,
        report_profile_screen_factory: ReportProfileScreenFactory | None = None,
    ) -> None:
        super().__init__(name="submission")
        self.repository = repository
        self.locale = locale
        self.service = service
        self.verifier_factory = verifier_factory
        self.report_profile_screen_factory = report_profile_screen_factory
        self.summaries: tuple[object, ...] = ()
        self.selected_index = 0
        self.plan: object | None = None
        self.result: object | None = None
        self.state: Literal[
            "select", "planning", "preflight", "preparing", "result", "verifying"
        ] = "select"
        self.phase = ""
        self.error = ""

    @property
    def is_working(self) -> bool:
        return self.state in {"planning", "preparing", "verifying"}

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self.locale("submission.title"), id="submission-title", markup=False),
            VerticalScroll(
                Static(id="submission-content", markup=False),
                id="submission-scroll",
            ),
            Horizontal(
                Button(
                    self.locale("submission.confirm"), id="submission-confirm", variant="primary"
                ),
                Button(self.locale("submission.adjust_report"), id="submission-adjust-report"),
                Button(self.locale("submission.verify"), id="submission-verify"),
                Button(self.locale("submission.back"), id="submission-back"),
                id="submission-actions",
            ),
            Static(id="submission-status", markup=False),
            Static(self.locale("submission.footer"), id="submission-footer", markup=False),
            id="submission-layout",
        )

    def on_mount(self) -> None:
        self._load_summaries()
        self._refresh()
        self.call_after_refresh(self._focus_primary)

    def on_resize(self, event: object) -> None:
        del event
        self.call_after_refresh(self._refresh)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "submission-confirm": self.action_activate,
            "submission-adjust-report": self.action_adjust_report,
            "submission-verify": self.action_verify,
            "submission-back": self.action_back,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()

    def action_previous(self) -> None:
        if self.state != "select" or not self.summaries:
            return
        self.selected_index = (self.selected_index - 1) % len(self.summaries)
        self._refresh()

    def action_next(self) -> None:
        if self.state != "select" or not self.summaries:
            return
        self.selected_index = (self.selected_index + 1) % len(self.summaries)
        self._refresh()

    def action_activate(self) -> None:
        if self.is_working:
            return
        if self.state == "select":
            self._start_plan()
        elif self.state == "preflight" and self.plan is not None:
            if getattr(self.focused, "id", None) == "submission-adjust-report":
                self.action_adjust_report()
            elif not self._is_blocked():
                self._start_prepare()

    def action_verify(self) -> None:
        if self.state != "result" or self.result is None:
            return
        self.state = "verifying"
        self.phase = "verifying"
        self._refresh()
        self.run_worker(
            self._verify_worker(self.result),
            name="submission-verify",
            group="submission",
            exclusive=True,
            exit_on_error=False,
        )

    def action_adjust_report(self) -> None:
        if self.state != "preflight" or self.plan is None or self.is_working:
            return
        factory = self.report_profile_screen_factory
        if factory is None:
            self.error = "请返回“整理证据”后配置报告结构。"
            self._refresh()
            return
        try:
            screen = factory(str(getattr(self.plan, "evidence_set_id", "")))
        except Exception:
            self.error = "报告配置暂时无法打开，请返回“整理证据”后重试。"
            self._refresh()
            return
        self.app.push_screen(screen)

    def action_back(self) -> None:
        if self.is_working:
            return
        if self.state == "select":
            self.app.pop_screen()
            return
        if self.state == "preflight":
            self.state = "select"
            self.plan = None
        elif self.state in {"result", "preparing", "verifying"}:
            self.state = "preflight" if self.plan is not None else "select"
        self.error = ""
        self._refresh()

    def _load_summaries(self) -> None:
        repository = self.repository
        if repository is None:
            repository = EvidenceSetRepository.from_cwd(Path.cwd())
        try:
            values = repository.list_summaries()
            self.summaries = tuple(values)
        except Exception as error:
            self.summaries = ()
            self.error = self._safe_error(error, "证据集暂时无法读取，请返回并重试。")

    def _start_plan(self) -> None:
        if not self.summaries:
            return
        summary = self.summaries[self.selected_index]
        evidence_id = getattr(summary, "evidence_set_id", "")
        if not evidence_id:
            return
        self.state = "planning"
        self.phase = "checking"
        self.error = ""
        self._refresh()
        self.run_worker(
            self._plan_worker(evidence_id),
            name="submission-plan",
            group="submission",
            exclusive=True,
            exit_on_error=False,
        )

    async def _plan_worker(self, evidence_id: str) -> None:
        try:
            plan = await asyncio.to_thread(self.service.plan, evidence_id)
        except Exception as error:
            self.state = "select"
            self.error = self._safe_error(error, "提交预检失败，请检查证据集和报告配置。")
        else:
            self.plan = plan
            self.state = "preflight"
            self.phase = ""
        self._refresh()
        self.call_after_refresh(self._focus_primary)

    def _start_prepare(self) -> None:
        if self.plan is None or self.is_working:
            return
        self.state = "preparing"
        self.phase = "checking"
        self.error = ""
        self._refresh()
        self.run_worker(
            self._prepare_worker(self.plan),
            name="submission-prepare",
            group="submission",
            exclusive=True,
            exit_on_error=False,
        )

    async def _prepare_worker(self, plan: object) -> None:
        try:
            value = await asyncio.to_thread(self._prepare, plan)
            if inspect.isawaitable(value):
                value = await value
        except Exception as error:
            self.state = "preflight"
            self.error = self._safe_error(error, "提交材料准备失败，请处理阻塞项后重试。")
        else:
            self.result = value
            self.state = "result"
            self.phase = "complete"
        self._refresh()
        self.call_after_refresh(self._focus_primary)

    def _prepare(self, plan: object) -> object:
        return self.service.prepare(
            plan,
            force=True,
            phase_callback=self._phase_from_worker,
        )

    def _phase_from_worker(self, phase: str) -> None:
        self.app.call_from_thread(self._set_phase, str(phase))

    def _set_phase(self, phase: str) -> None:
        self.phase = phase
        self._refresh()

    async def _verify_worker(self, result: object) -> None:
        try:
            directory = getattr(result, "destination", result)
            verification = await asyncio.to_thread(self.verifier_factory().verify, directory)
            self.error = (
                "校验通过。"
                if getattr(verification, "verified", False)
                else "校验未通过，请检查提交目录。"
            )
        except Exception as error:
            self.error = self._safe_error(error, "独立校验失败，请检查提交目录。")
        self.state = "result"
        self.phase = ""
        self._refresh()

    def _focus_primary(self) -> None:
        button_id = (
            "submission-confirm" if self.state in {"select", "preflight"} else "submission-verify"
        )
        try:
            button = self.query_one(f"#{button_id}", Button)
            button.focus()
        except Exception:
            return

    def _refresh(self) -> None:
        if not self.is_attached:
            return
        content = self._render_content()
        self.query_one("#submission-content", Static).update(content)
        status = self.phase_label(self.phase) if self.is_working else self.error
        self.query_one("#submission-status", Static).update(self._fit(status, 80))
        confirm = self.query_one("#submission-confirm", Button)
        verify = self.query_one("#submission-verify", Button)
        back = self.query_one("#submission-back", Button)
        confirm.disabled = (
            self.is_working
            or self.state == "result"
            or (self.state == "preflight" and self._is_blocked())
        )
        self.query_one("#submission-adjust-report", Button).display = self.state == "preflight"
        verify.display = self.state == "result"
        verify.disabled = self.is_working
        back.disabled = self.is_working

    def _render_content(self) -> str:
        if self.state in {"select", "planning"}:
            lines = ["选择一份证据集", ""]
            if not self.summaries:
                lines.append("暂无可用证据集，请先整理证据。")
            for index, summary in enumerate(self.summaries):
                marker = ">" if index == self.selected_index else " "
                title = self._safe(getattr(summary, "title", "未命名证据集"))
                count = getattr(summary, "item_count", None)
                lines.append(
                    f"{marker} {title}  |  证据 {count if count is not None else '未知'} 条"
                )
            return self._fit("\n".join(lines), 80)
        if self.state == "preflight" and self.plan is not None:
            return self._render_preflight(self.plan)
        if self.state in {"preparing", "result"} and self.result is not None:
            return self._render_result(self.result)
        if self.state == "preparing":
            return "正在准备提交材料，请稍候。"
        return self._fit(self.error, 80)

    def _render_preflight(self, plan: object) -> str:
        check = getattr(plan, "check", None)
        status = getattr(check, "status", "SKIP")
        marker = {"PASS": "[OK]", "WARN": "[WARN]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}.get(
            str(status), "[SKIP]"
        )
        sources = getattr(plan, "resolved_sources", ())
        lines = ["提交预检", f"证据集：{self._safe(getattr(plan, 'evidence_set_id', ''))}", ""]
        archive_marker = (
            "[OK]"
            if getattr(getattr(plan, "project_archive", None), "verified", False)
            and not self._pack_blockers(plan)
            else "[FAIL]"
        )
        evidence_marker = (
            "[FAIL]"
            if any(getattr(source, "available", True) is False for source in sources)
            else "[OK]"
        )
        lines.extend(
            (
                f"{evidence_marker} 证据来源  {len(sources)} 条",
                "[OK] 报告配置  已读取",
                f"{marker} 项目检查  {getattr(check, 'warning_count', 0)} 条提示",
                f"{archive_marker} 项目打包  已验证",
                f"[OK] 输出位置  {self._safe(getattr(plan, 'destination', ''))}",
                "",
            )
        )
        warnings = tuple(getattr(plan, "warnings", ()))
        blockers = tuple(getattr(plan, "blockers", ()))
        if self._pack_blockers(plan) and not any(
            "项目打包存在阻塞项" in str(value) for value in blockers
        ):
            blockers += ("项目打包存在阻塞项，请返回“整理证据”或检查项目后重试。",)
        if any(getattr(source, "available", True) is False for source in sources):
            blockers += ("证据来源不可用，请返回“整理证据”修复或移除。",)
        if warnings:
            lines.append("提示：")
            lines.extend(f"[WARN] {self._safe(value)}" for value in warnings)
        if blockers:
            lines.append("需要处理：")
            lines.extend(f"[FAIL] {self._safe(value)}" for value in blockers)
        lines.append("Enter 确认准备；Esc 返回选择")
        return self._fit("\n".join(lines), 80)

    def _render_result(self, result: object) -> str:
        lines = [
            "提交材料已准备",
            "",
            f"输出位置：{self._safe(getattr(result, 'destination', ''))}",
            f"{Path(str(getattr(result, 'report_path', 'report.docx'))).name}",
            f"{Path(str(getattr(result, 'archive_path', 'student-project.zip'))).name}",
            f"{Path(str(getattr(result, 'manifest_path', 'submission-manifest.json'))).name}",
            f"验证状态：{'已验证' if getattr(result, 'verified', False) else '未验证'}",
            "",
            "清单是校验收据，除非任课教师要求，否则无需单独提交。",
            "请手动上传材料；CSBox 不会替你提交。",
        ]
        warnings = tuple(getattr(result, "warnings", ()))
        if warnings:
            lines.extend(("", "警告：", *(f"[WARN] {self._safe(value)}" for value in warnings)))
        return self._fit("\n".join(lines), 80)

    def _is_blocked(self) -> bool:
        readiness = getattr(self.plan, "readiness", None)
        return (
            bool(getattr(self.plan, "blockers", ()))
            or self._pack_blockers(self.plan)
            or self._sources_unavailable(self.plan)
            or str(readiness) == str(SubmissionReadiness.BLOCKED)
        )

    @staticmethod
    def _sources_unavailable(plan: object) -> bool:
        return any(
            getattr(source, "available", True) is False
            for source in getattr(plan, "resolved_sources", ())
        )

    @staticmethod
    def _pack_blockers(plan: object) -> bool:
        pack_plan = getattr(plan, "pack_plan", None)
        blockers = getattr(pack_plan, "blockers", ())
        if callable(blockers):
            blockers = blockers()
        return bool(blockers) or bool(getattr(pack_plan, "rejected", ()))

    def phase_label(self, phase: str) -> str:
        labels = {
            "checking": "正在检查",
            "reporting": "正在生成报告",
            "packing": "正在打包项目",
            "verifying": "正在校验",
            "publishing": "正在输出",
            "complete": "已完成",
        }
        return labels.get(phase, "正在准备提交材料")

    def _fit(self, value: str, width: int) -> str:
        return "\n".join(
            wrap_cells(value, max(2, min(width, self.size.width - 6 if self.size.width else width)))
        )

    @staticmethod
    def _safe(value: object) -> str:
        return truncate_cells(str(value), 160, ellipsis="…")

    @staticmethod
    def _safe_error(error: Exception, fallback: str) -> str:
        return str(getattr(error, "user_message", "")) or fallback


__all__ = ["SubmissionScreen"]
