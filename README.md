# AI 开发治理体系

面向 Codex 与多智能体协作开发的社区通用治理资源。当前社区基线为 **v3.0.2**：继承 v3.0.1 的开发者标识、测试失败归因、环境等待恢复、冻结验证计划受控纠正及版本绑定，新增经人工新 G1 确认的业务重基线与旧控制精确替代。

本仓库只包含治理体系，不包含任何业务项目源码、真实需求、需求测试记录、回放样本、账号凭据或本机环境资料。

## 核心能力

- G1 需求确认、G2 执行就绪与 UCR 冻结后变更边界；
- `BaselineSupersession` 重基线：保留历史原文与事实、禁止复用旧候选、重新规划并取得人工 G2；
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
docs/治理版本说明/              v3.0.1、v3.0.2 增量与兼容说明
docs/需求版本管理.md             跨需求导航空模板
scripts/governance_tools.py     校验、编号、投影与恢复工具
scripts/governance_package.py   固定发行构建与离线完整性校验
scripts/governance_project.py   接入/升级预览、应用与需求版本选择
scripts/governance_start.py     项目统一启动、可靠定位、续接和只读查询
plugin.json、skills/            Codex 插件清单与技能入口（开发候选）
scripts/tests/                  仅使用合成数据的工具自检
AGENTS.md                       社区版接入入口
```

## 接入项目

新接入的开发候选已实现“社区产品源码 → 固定版本技能插件 → 项目配置与适配”：构建离线校验包，再生成并应用接入预览，保留项目 AGENTS 和定制。升级做三方比较，旧需求继续使用其固定发行。具体命令和限制见[项目接入与升级](docs/工具说明/项目接入与升级.md)，职责见[社区插件与项目适配](docs/工具说明/社区插件与项目适配.md)。当前插件版本 `0.1.0-dev.7`，治理/测试版本分别保持 `v3.0.2` / `v3.0.1`。已验证原生加载、B1、自然语言需求登记、退出后普通开发和明确恢复；公司隔离工作树适配保留旧资产与直接开发例外。桌面 UI、公司全量业务验证和生产服务不在已验证范围。

新增[需求管理增量协议 v1.0](docs/工具说明/需求管理.md)：从新批次登记需求、G2 后分配迭代号、读取已上传分支生成总表，支持单需求持久退出/恢复及串行交付历史。总表不替代门禁或最终归档事实；不自动提交、推送或迁移历史需求。

新增[统一启动与续接](docs/工具说明/统一启动与续接.md)：AI 用一个入口完成需求定位、模式检查、原发行状态查询和总表刷新；新建请求可安全重试，分支与本机关联支持续接，纯查询不写入。启动不代替门禁，不提前执行测试。

新增[模板收敛](docs/工具说明/模板收敛.md)：按需求绑定只读定位五类通用模板，项目可声明 RELEASE_ONLY 停止生成重复模板正文；历史记录和引用先核对，旧路径可保留简短导航，固定发行保持完整。

以下副本式接入继续兼容，既有项目不自动迁移：

1. 将 `.codex/`、`docs/Agent治理/`、`docs/开发规范/`、`docs/治理版本说明/`、`docs/需求版本管理.md`、`scripts/governance_tools.py` 和 `AGENTS.md` 复制到目标 Git 仓库。
2. 在目标仓库的 `AGENTS.md` 中声明 v3.0.2 的启用日期、适用需求及项目交付路径；阅读 [v3.0.2 治理修订说明](docs/治理版本说明/v3.0.2治理修订说明.md)，分别绑定治理 v3.0.2 与测试规范 v3.0.1。
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

## 可选执行工具与规则组合（开发候选）

本轮 B1 主要服务于本地测试执行前。[本地测试准备入口](docs/工具说明/本地测试准备.md)从既有计划与上下文包读取本轮测试，复用项目准备脚本，支持检查、自动准备、就绪子集执行衔接及临时资源清理。它是未发布候选，不生成正式 TEST_READINESS、QA 或门禁事实；STANDARD / CONTROLLED 继续通过现有运行时派发独立 QA。

```powershell
python scripts/governance_local_tests.py --repo . --requirement-id REQ-DEV-20261028-01
```

以上是排查时的手动预览入口。项目采用候选并在已有配置启用 Automatic=true 后，根 AGENTS 与 Writer/QA 角色入口要求主智能体在测试前自动调用，开发者无需逐次输入命令。STANDARD / CONTROLLED 使用短期环境会话保持 QA 所需服务和数据存活；执行及一次性项目入口适配见工具说明。[原只读诊断工具](docs/工具说明/环境诊断.md)继续支持无需清单的项目元数据发现和旧清单核对，作为辅助能力保留：

```powershell
python scripts/governance_environment.py --repo .
python scripts/governance_environment.py --repo . --scope frontend
```

需要核对明确的需求归属、资源与逐项准备声明时，可继续使用：

```powershell
python scripts/governance_environment.py --repo . --manifest .parallel/governance-environment.local.json --requirement-id REQ-DEV-20261019-01 --phase planning --owner QA
```

固定发行接入会提供候选工具的小入口并转发到锁定发行；副本式项目需额外复制 `scripts/governance_environment.py`、`scripts/governance_local_tests.py`、`scripts/governance_test_session.py` 与 `docs/工具说明/`，合并根 AGENTS 的候选自动调用入口和对应 Writer/QA 增量，并保留同目录的 `governance_tools.py`。只读诊断仍不执行命令或启停服务；自动调用由已采用候选的主智能体/角色触发，不要求用户逐次执行，也没有后台全项目扫描。已发布 v3.0.2 规则保持原样，候选的本机自测不能代替目标项目真实试用。

新增候选能力按需选择，工具说明包含接口、自动调用位置和验证边界：

| 能力 | 入口 | 结果 |
|---|---|---|
| B2 原始字节候选快照 | [governance_candidates.py](scripts/governance_candidates.py)、[说明](docs/工具说明/候选快照.md) | 固定精确交付集合、交接与治理输入；生成待运行时登记的候选草稿 |
| B3 只读状态与修复导航 | [governance_status.py](scripts/governance_status.py)、[说明](docs/工具说明/状态查询.md) | 核验当前候选，显示支持的阶段、缺项和下一动作；未知事实保持无法判定 |
| C1–C3 可组合规则 | [governance_rules.py](scripts/governance_rules.py)、[说明](docs/工具说明/规则组合.md) | 固定核心、可选规则包和项目来源，生成现有 ApplicableRules 表格预览 |

规则样例覆盖[纯后端](examples/rules/backend-only/project.json)、[Vue 全栈](examples/rules/fullstack-vue/project.json)、[React 全栈](examples/rules/fullstack-react/project.json)。它们是合成配置，不代表项目已实际安装或运行这些技术栈。规则组合的项目声明由项目维护，固定发行携带通用 `rulepacks/`；副本式接入需自行携带规则包和依赖工具。B3 复用 B1 的只读解析函数和 B2 核验入口。社区新工具不会自动安装到业务仓。

C4/C5 已形成可验证的制品、项目运行器与升级候选；B4 中间计划前驱、其他 AI 工具适配和完整自动续接仍属后续工作。本轮没有移动稳定 main/tag、发布新治理版本或迁移历史需求。

## 采用边界

- 社区基线不会自动在目标项目生效；项目必须显式批准并记录版本绑定。
- 不自动提交、推送、合并、发布或部署。
- 不默认假设 CI、生产平台、审批系统或外部服务存在。
- 历史需求继续按已绑定版本解释，不因复制本仓库而自动迁移。
- 执行中需求迁移需要逐项批准；重基线校验通过不等于业务语义获批、G2 已形成、QA 通过或人工业务验收完成。
- 校验与投影工具不是完整自动执行引擎；不会自动分配工作树、生成候选、派发角色或推进所有门禁。

## 许可证

[Apache License 2.0](./LICENSE)
