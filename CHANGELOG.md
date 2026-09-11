# Changelog

## 0.7.0

- **Submission Handoff / 准备提交:** complete the beginner-first `Record → Evidence → Prepare Submission → Verify → manual upload` path. A coordinated preflight connects Evidence Set, Report Profile, Check, and a verified Pack archive without changing the source project, Evidence, or Profile data.
- **Final three-file handoff:** produce `report.docx`, the configured project ZIP, and `submission-manifest.json` in one small final directory. The manifest records artifact sizes and SHA-256 digests, including its own canonical self-digest.
- **Independent verification:** `csbox submit verify` verifies a copied or moved handoff using only its final contents, including the nested Pack manifest and project-archive integrity; it does not require the original project or a network connection.
- **Safe refresh and stale-state protection:** `--force` refreshes only a demonstrably CSBox-owned, unchanged destination. Changes to Evidence, Report Profile, Check, Pack inputs, or destination state reject stale plans before publication.
- **Atomic and rollback safety:** stage and verify the complete handoff before publication, use safe destination ownership checks and locking, and preserve the previous owned handoff when a refresh cannot complete.
- **CLI + TUI:** expose the workflow through `csbox submit` and Home → `准备提交`, with visible READY/WARNING/BLOCKED preflight states, recovery guidance, and CJK/responsive layout support.
- **Reusable Pack verification:** share `PackArchiveVerifier` for standalone and nested ZIP verification, with strict manifest schema, safe paths, archive entry integrity, and redacted public errors.
- **Product boundaries:** Lab Evidence, API Evidence, Evidence Collection, Report Handoff, Check, and Pack remain local-first helpers for recording, checking, organizing, and delivering student-provided work. **CSBox does not upload coursework.** It does not generate coursework conclusions, answers, or prose; the user manually uploads the verified handoff.

## 0.6.1

- **Open-source project surface:** add a concise contributor guide, structured issue forms, and pull request guidance.
- **Contribution workflow:** document targeted testing, Ruff and formatting checks, CJK/display-width expectations, supported platforms, security boundaries, and academic integrity.
- **GitHub metadata:** refresh the repository description, homepage, topics, and historical pull request housekeeping.
- **Package project URLs:** expose the canonical repository, issue tracker, and changelog links in distribution metadata.
- **Release presentation:** make the release-note contract explicit and improve the public v0.6.0 and v0.6.1 release descriptions.
- **Public documentation:** link both README variants to `CONTRIBUTING.md` and update the current stable version to 0.6.1.

## 0.6.0

- **Release:** v0.6.0 is the current stable release.
- **Unified Evidence:** add typed Lab Capture and saved API Run Step sources to one ordered Evidence Set, with deterministic v1 read / v2 save compatibility.
- **Mixed Reports:** resolve both canonical source families through their existing safe renderers and preserve Evidence Set order in Markdown, DOCX, and assets.
- **Documentation:** make Simplified Chinese the default README, add the full English README, and use PyPI-safe absolute language and screenshot links.

## 0.5.1

- **Lab & Review:** record a continuous terminal session, capture point-in-time evidence with F12, and review, edit, delete, or export completed records.
- **Evidence & Reports:** organize canonical Capture sources into Evidence Sets and export user-provided material as Markdown, DOCX, and image assets without generating coursework.
- **Check & Safe Pack:** inspect project structure and delivery risks, keep WARN findings visible and non-blocking, and build verified ZIPs with fail-closed inventory and ownership protections.
- **Beginner TUI & Help:** make Home, Help, Lab, Records, Evidence, Check, Pack, Report, and API workflows discoverable with consistent terminology and keyboard guidance.
- **Documentation & Installation:** provide aligned English and Simplified Chinese guidance, current UI screenshots, and installable wheel/sdist artifacts for Python 3.11+.
- **Windows / Reliability:** validate the supported Windows terminal, PowerShell, ConPTY, CJK layout, and cross-platform packaging paths in automated CI.

## 0.5.0rc1

- 完成 Lab Evidence、API Evidence、Project Check 和 Safe Pack 四条基础交付工作流。
- 增加 Evidence Collection、Report Handoff 和 Course Report Formatting，支持按用户保存的结构导出 Markdown、DOCX 和图片材料。
- 保持本地优先和学术诚信边界：CSBox 整理、格式化和导出用户撰写的材料，不生成实验分析、结论或课程答案。

当前 Release Candidate 尚未发布到 PyPI。

## 0.3.0

- 完善 Lab 生命周期、Windows terminal 可靠性和 F12 Capture 反馈。
- 增加实验 Records、Review、Export 及自定义导出位置。
- 完善 beginner-first Home / Start、键盘输入、首次运行、帮助和安装体验。

## 0.2.0

- 增加 API 场景、运行记录、脱敏 Evidence 导出和 OpenAPI 模板导入。
- 增加项目 Check、Pack dry-run、manifest 和可重复结构校验。
- 完善 Linux/Windows terminal、CJK、resize、Capture、replay 和 Textual TUI smoke。
- 提供可安装的 wheel/sdist、`csbox` console entrypoint 和安装后资源验证。

本版本是项目当前开发版本，未声明已发布到 PyPI。

## 0.1.0

- 建立本地终端实验记录、session/replay、Capture 和 PNG/Markdown 证据导出的 usable core。
