# Security Boundaries

CSBox 的目标是帮助本地学习项目整理证据和交付文件，不是秘密管理器，也不会替用户判断未知数据是否绝对安全。

## 当前默认行为

- API transport 默认启用 TLS verification，并默认不跟随 redirect。
- API 运行记录、Evidence、TUI 和日志只消费脱敏视图。
- `csbox check` 检查环境文件、私钥、轻量 hard-coded secret、路径泄露、缓存和构建产物；敏感发现只显示相对路径、行号和类别。
- `csbox check --deep` 仅在本机已安装 `gitleaks` 时运行；工具不存在时返回可解释的 `SKIP`。
- `csbox pack` 拒绝真实 `.env`、私钥、路径 traversal、危险符号链接和不安全 archive path，并使用临时 staging。
- session、API runs、Evidence 和 ZIP 都是本地文件；不要把真实凭据写入场景、session 或项目示例。

## 使用边界

请在提交或分享之前检查 `csbox check --plain` 和 `csbox pack --dry-run --plain` 的结果。深度扫描工具不是核心 runtime dependency，安装 `gitleaks` 后再使用 `--deep`。如果发现未知 secret、依赖漏洞或平台权限问题，请人工复核；本地 Check 不能替代组织级安全审计。
