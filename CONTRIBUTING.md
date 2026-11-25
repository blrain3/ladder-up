# 参与贡献

本项目当前处于设计阶段（文档 + UML 图），暂无代码。欢迎以下形式的贡献：

- 指出需求基线中的口径矛盾或遗漏（对照 `docs/需求基线-v1.2.md`）
- 指出数据库设计与接口设计不一致之处
- 补充测试用例（`docs/测试计划-v1.0.md` 的覆盖矩阵）
- 改进 UML 图（`diagrams/src/*.puml`）

## 流程

1. 开 issue 说明问题，附对应文档章节号或图编号。
2. 改动文档：一个 PR 只改一类内容（需求 / 设计 / 测试）。
3. 改了 `.puml` 的，先跑 `make diagrams`，把 PNG 与源文件一起提交。
4. 改了文档的，跑 `make docs` 确认能生成 docx/pdf。
5. 提交信息格式见 `docs/工程规范.md` 第 4 节。

## 约定

- 数据表、接口、验收标准以 `docs/数据库设计-v1.0.md` 与 `docs/接口设计-v1.0.md` 为准；与需求基线冲突时以基线为准，并在 PR 中说明。
- 不提交真实成员数据、凭据、校内系统截图。
- 图表用 PlantUML，不提交手工导出的图片替代图源。

### 浏览器端到端冒烟测试

首次运行时，在项目虚拟环境中安装开发依赖和 Chromium：

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
make e2e
```

Linux 环境如缺少 Chromium 系统依赖，可执行
`python -m playwright install --with-deps chromium`。需要查看每条用例耗时时，
运行 `.venv/bin/pytest tests/e2e -q --durations=0`。

测试使用无头 Chromium，并自行启动监听回环随机端口的 Uvicorn。数据库和上传目录均为测试临时目录。
默认收集通过 `--ignore=tests/e2e` 跳过浏览器测试，因此 `make test` 和 `make check` 也会跳过它。
CI 的独立 `e2e` job 仅在推送到 `main` 时执行。
