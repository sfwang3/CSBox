# Changelog

## 0.7.0rc1 (release candidate)

- **Submission Handoff:** add the beginner-first `Record → Evidence → Prepare Submission → Verify → manual upload` product path. One preflight and one coordinated transaction produce `report.docx`, the configured project ZIP, and `submission-manifest.json` without changing source project, Evidence, or Profile data.
- **Integrity and boundaries:** expose READY/WARNING/BLOCKED readiness, preserve WARN/FAIL/SKIP truth, verify copied or moved handoffs offline, and keep CSBox local-first: prepared is not submitted, and CSBox never uploads.
- **Documentation:** publish the Submission Handoff contract and distinguish the `0.7.0rc1` source candidate from the PyPI `0.6.1` stable release; the RC is not published to PyPI.

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
