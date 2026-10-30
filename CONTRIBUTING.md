# 贡献指南

欢迎提交问题和改进建议。修改治理规范时请保持单一目标，并同时说明变更原因、兼容性、对历史需求的影响和验证结果。

提交前请执行：

社区维护使用仓库根 `environment.yml` 定义的独立 Conda 环境。首次创建，后续进入已有环境即可：

```powershell
conda env create --file environment.yml
conda activate governance-dev
```

该环境固定 Python 与维护阶段使用的 PyYAML，并启用 UTF-8 和禁止生成字节码。治理工具及现有 unittest 测试本身使用标准库；PyYAML 用于 Skill 的 `quick_validate.py` 等维护检查，不随插件分发，也不加入业务项目依赖。Git、Codex CLI 及业务项目的 Node、JDK、Maven 等工具按各自配置使用，不由这份环境声明安装或升级。

激活后执行以下检查；自动化或 AI 不依赖当前终端的激活状态，可用 `conda run --name governance-dev --no-capture-output python ...` 明确选择同一解释器：

```powershell
python scripts/governance_tools.py check --repo .
python -m unittest discover -s scripts/tests -p "test_governance*.py" -v
```

修改 Skill 时，还应使用该环境执行当前安装的 `skill-creator/scripts/quick_validate.py`，参数为本仓库 `skills/development-governance`。校验器属于维护工具，不复制到固定发行；不要因 Python 默认指向其他环境而跳过缺失依赖。

后续确实需要新的维护依赖时，先确认用途和版本，再加入这一个环境并同步 `environment.yml`，用原检查验证；不预装尚未使用的框架、SDK 或业务测试依赖。`conda env export --name governance-dev --from-history` 可核对直接依赖；同平台精确重建可另存 `conda list --name governance-dev --explicit` 的快照。导出文件提交前移除本机 prefix、凭据及其他本地信息，不把平台专用快照当作跨平台锁文件。

请勿提交真实需求目录、业务源码、凭据、Cookie、Session、本机绝对路径、浏览器资料、回放样本或生产数据。治理规则发生语义变化时必须提高版本，不得原位改写已冻结版本后继续沿用原版本号。
