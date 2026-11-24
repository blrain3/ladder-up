# 前端样式迁移代码评审

| 版本 | 日期 | 来源 | 状态 |
|---|---|---|---|
| v1.0 | 2026-11-24 | `fix/ui-bootstrap5` 工作区与 HEAD（`7a12e20`）对比 | 修完再合并 |

## 功能核对

- 逐文件检查基模板及全部 30 个子模板。实际清单比请求所列多两个子模板。33 处表单的字段名、`action`、`method`、编码及隐藏 CSRF 字段与基线一致，未发现嵌套表单。知识点操作区用同一容器排列两个同级表单，同时核对了照片上传仍使用 multipart 编码。抽查包括退出、登录、申请、审核、面试、录取、照片上传和删除、知识点移动和删除、录分、任务下发（`app/templates/base.html:129`、`app/templates/login.html:6`、`app/templates/apply.html:7`、`app/templates/applications.html:39,50,61`、`app/templates/member.html:31,40`、`app/templates/knowledge_points.html:40,45`、`app/templates/assessment_scores.html:7`、`app/templates/task_detail.html:17`）。
- 导航调用 `allows`，每次 HTML 认证请求按当前角色和授权重新计算权限。页面测试覆盖会员、教练、系统管理员以及同一会话授权后的变化，原地切换角色未核对。直接访问统计仍有 403 断言，全量导出也有独立拒绝断言（`app/templates/base.html:66`、`app/auth.py:60,112`、`tests/test_ui.py:29,40,48`、`tests/test_profile_pages.py:79`、`tests/test_profile_csv.py:102`）。
- 知识点删除、照片删除和任务下发已有确认标记，关闭 JS 仍可原生提交，服务端继续校验 CSRF 和权限。删除题目和除名已有 API，迁移前后均未见页面提交入口。撤销权限或授权未见相应路由或模板入口，是否要求新增入口未核对。后续接入时，应将确认绑定实际发起变更的提交控件，并保留接口现有的权限和审计校验（`app/templates/knowledge_points.html:45`、`app/templates/member.html:40`、`app/templates/task_detail.html:17,37`、`app/auth.py:119,124`、`app/routes.py:1432,2565`、`app/auth.py:64`）。

## Bootstrap、资源与脚本

- `rg` 搜索 `navbar-default|btn-default|panel-|form-group|help-block|text-right|pull-right|label label-|table-condensed`，全模板零匹配。折叠按钮的 BS5 属性对应 `#main-nav`，提示关闭也用 `data-bs-dismiss`。124 个 Bootstrap Icons 均标有 `aria-hidden="true"`，按钮和状态保留文字。折叠与关闭按钮使用可访问名称。已有 `label for` 均能找到对应 `id`，错误保留 `role="alert"`，28 张表均未设置表名（`app/templates/base.html:12,57,153,163`、`app/templates/login.html:8`、`app/templates/error.html:5`、`app/templates/members.html:7`）。
- 本地 Bootstrap 5.3.3 的 CSS 和 bundle 分别为 232803、80721 字节，Icons 1.11.3 的 CSS 和 WOFF2 分别为 85814、130396 字节。字体 URL 均为 `/static/bootstrap-icons.woff2`，Bootstrap CSS 使用内嵌 SVG。模板与 CSS 无 CDN、远程字体或其他外部加载地址。CSS 中的外链字符串仅见于许可证注释和 SVG 命名空间（`app/templates/base.html:53,169`、`app/static/bootstrap.min.css:1,6`、`app/static/bootstrap.bundle.min.js:1`、`app/static/bootstrap-icons.css:2,5`）。
- `app.js` 仅将提示传入原生 `confirm`，无 `innerHTML` 或 `eval`。录分的表单 ID、字段和 `formdata` 过滤逻辑保持一致。模板脚本均通过本地 `src` 加载，浏览器实际事件行为未核对（`app/static/app.js:2`、`app/static/assessment-scores.js:1`、`app/templates/assessment_scores.html:7,17,43`、`app/templates/base.html:169`）。

## 测试与约定

- 新测试核对角色入口链接及本地资源路径，但存在三处典型漏检。删掉各列表的图标，只留主导航图标，仍通过页面图标断言。把确认标记移到标题仍通过删除与下发断言。删掉字体和 Bootstrap bundle 文件仍可通过资源测试（`tests/test_ui.py:63,81,91,94`）。
- 相关页面、权限和导出共 111 个 pytest 用例通过。搜索分页测试仍校验完整查询参数。考核和档案允许「不通过」匹配「通过」，审核状态从单元格放宽为全文，知识点测试从按钮文字改成全文确认文案。烟测仍保留 CSRF、一次性密码、题面、训练和档案断言，运行在连接本地服务时中断（`tests/test_problem_search.py:137`、`tests/test_assessment_pages.py:45`、`tests/test_profile.py:45`、`tests/test_review.py:120`、`tests/test_taxonomy.py:135`、`scripts/smoke.sh:39,87,129,236,309,402`）。
- Issue #1 无法获取，未核对。工作区缺少 `docs/adr/0004-前端样式与组件库.md`，未核对。`docs/工程规范.md` 强制合并前通过烟测，现有版本未见前端新增约定。禁 CDN 与危险确认按本次评审要求核查，规范一致性未核对（`tests/test_ui.py:1`、`docs/工程规范.md:57,60,73`）。

## 必须修（4 条）

1. 恢复普通会员奖惩入口。路由允许 `member:read:self` 查询本人奖惩，模板却要求 `member:read`，并将旧测试改成期望隐藏。恢复两种权限均可见，并断言本人入口和数据范围（`app/templates/base.html:115`、`app/routes.py:2695,2702`、`tests/test_profile_pages.py:85`）。
2. 修复两处必败烟测。会员证学号断言应容许 `dd` 带 class。题面 XSS 断言应限定题面容器，继续禁止实际脚本节点及事件属性，同时保留转义断言。同样收紧页面测试，当前仅禁止一条完整脚本文字（`scripts/smoke.sh:172,236`、`app/templates/card.html:23`、`app/templates/base.html:169`、`app/templates/problem.html:20`、`tests/test_problem_xss.py:28`）。
3. 确认应读取实际提交按钮。目前同一表单的任一后代带标记，其他按钮也会弹窗，多个危险按钮会共用首个提示。隔离执行监听器已复现两种误判，也验证了单一按钮和表单标记在取消时均能阻止提交。用 `event.submitter?.dataset.confirm` 并回退到表单标记，覆盖键盘提交、取消与普通按钮（`app/static/app.js:2`）。
4. 补齐两套 MIT 版权和许可全文。目前清单只有版本、许可名和链接，原许可文件已删除。随分发资产附上对应版本的许可全文，并核对 bundle 中 Popper 的版权及许可声明（`app/static/VENDOR-LICENSES.txt:1`、`app/static/bootstrap.min.css:2`、`app/static/bootstrap-icons.css:3`）。

## 建议修

- 给列表和档案各表提供 `caption` 或可访问名称，为逐行录分、审核意见及部门选择补标签，逐题勾选使用 `fieldset` 和 `legend`（`app/templates/profile.html:27,45,70,94`、`app/templates/assessment_scores.html:25,29`、`app/templates/applications.html:42,53,64`、`app/templates/checkin.html:15`）。
- 把打印按钮的内联 `onclick` 移到本地 JS，打印样式移到本地 CSS。严格 CSP 下同时处理进度条的内联宽度。进度条可采用带可访问名称和值的原生进度元素（`app/templates/card.html:7,34`、`app/templates/training_progress.html:33,61`、`app/templates/task_detail.html:65`）。
- 按目标行、表单及单元格断言精确状态文本、图标和确认标记，并实际请求静态资产，覆盖折叠按钮目标和字体可用性（`tests/test_assessment_pages.py:45`、`tests/test_profile.py:45`、`tests/test_ui.py:18,63,81,94`）。

| 版本 | 变更 |
|---|---|
| v1.0 | 初次评审 |
