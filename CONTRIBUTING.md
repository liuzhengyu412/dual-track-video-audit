# Contributing

欢迎提交问题和改进。提交代码前请：

1. 不要提交 `.env`、真实审核规则、话术、视频、转写或审核结果。
2. 运行 `python3 run_audit.py --validate-config`。
3. 运行 `python3 -m unittest discover -s tests -v`。
4. 对模型适配、结果结构或配置格式的变更同步更新 README 和测试。
