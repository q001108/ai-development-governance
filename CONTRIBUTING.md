# 贡献指南

欢迎提交问题和改进建议。修改治理规范时请保持单一目标，并同时说明变更原因、兼容性、对历史需求的影响和验证结果。

提交前请执行：

```powershell
python scripts/governance_tools.py check --repo .
python -m unittest discover -s scripts/tests -p "test_governance*.py" -v
```

请勿提交真实需求目录、业务源码、凭据、Cookie、Session、本机绝对路径、浏览器资料、回放样本或生产数据。治理规则发生语义变化时必须提高版本，不得原位改写已冻结版本后继续沿用原版本号。
