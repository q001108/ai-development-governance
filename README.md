# AI 开发治理体系

面向 Codex 与多智能体协作开发的社区通用治理资源。当前社区基线为 **v3.0.1**：以 v3.0 主流程为基础，合并开发者标识、测试失败归因、环境等待恢复、冻结验证计划受控纠正及版本绑定增量。

本仓库只包含治理体系，不包含任何业务项目源码、真实需求、需求测试记录、回放样本、账号凭据或本机环境资料。

## 核心能力

- G1 需求确认、G2 执行就绪与 UCR 冻结后变更边界；
- `LEAN / STANDARD / CONTROLLED` 三档确定性治理；
- `BA / RB / QG / AD` 验收与控制事项分类；
- 不可变候选、QA、Reviewer、质量汇合、失败计数和恢复协议；
- ControlProfile、AcceptancePlan JSON Schema 与 Python 校验器；
- Codex Writer、QA、Reviewer、Orchestrator、Development Trace 角色配置；
- Windows、macOS、Linux 本地环境及项目自定义交付平台声明。

## 仓库结构

```text
.codex/agents/                  Codex 角色配置
docs/Agent治理/                 主流程、事实协议、模板与机器契约
docs/开发规范/                  通用开发总则与测试规范
docs/治理版本说明/              v3.0.1 增量说明
docs/需求版本管理.md             跨需求导航空模板
scripts/governance_tools.py     校验、编号、投影与恢复工具
scripts/tests/                  仅使用合成数据的工具自检
AGENTS.md                       社区版接入入口
```

## 接入项目

1. 将 `.codex/`、`docs/Agent治理/`、`docs/开发规范/`、`scripts/governance_tools.py` 和 `AGENTS.md` 复制到目标 Git 仓库。
2. 在目标仓库的 `AGENTS.md` 中声明 v3.0.1 的启用日期、适用需求及项目交付路径。
3. 在 `docs/开发规范/` 补充项目实际需要的前端、后端、数据、安全或设计规范；不要把模板示例当成项目事实。
4. 初始化当前开发者的本地治理标识：

   ```powershell
   python scripts/governance_tools.py init-developer --repo . --developer-id DEV
   ```

5. 执行结构与引用检查：

   ```powershell
   python scripts/governance_tools.py check --repo .
   ```

`DEV` 仅为示例。实际标识应使用代码托管平台用户名或团队登记的唯一短代号。初始化只写入本地 Git 配置，不修改提交署名。

## 工具自检

工具仅依赖 Python 标准库：

```powershell
python -m unittest discover -s scripts/tests -p "test_governance*.py" -v
```

这些自检使用临时仓库和合成需求标识，不包含或复现任何真实需求测试。

## 采用边界

- 社区基线不会自动在目标项目生效；项目必须显式批准并记录版本绑定。
- 不自动提交、推送、合并、发布或部署。
- 不默认假设 CI、生产平台、审批系统或外部服务存在。
- 历史需求继续按已绑定版本解释，不因复制本仓库而自动迁移。

## 许可证

[Apache License 2.0](./LICENSE)
