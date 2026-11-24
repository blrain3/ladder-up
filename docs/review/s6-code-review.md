# S6 奖惩、档案、CSV 导出与会员统计代码评审

**判定：修完再合并。必须修 8 条。** 审查 `feat/s6-profile` 当前工作树，包含未跟踪实现。依据为开发路线、接口、奖惩表及权限设计。TC-B-05/06 对应题解和题库统计，本次会员切片的相关验收未核对。临时 SQLite 库的 S6 与迁移测试共 24 项通过。完整烟测及恢复库导出抽样未核对。（docs/开发路线-v1.0.md:78、docs/接口设计-v1.0.md:99、docs/数据库设计-v1.0.md:169、docs/设计说明书-v1.0.md:143、docs/测试计划-v1.0.md:51、docs/测试计划-v1.0.md:68、tests/conftest.py:22、tests/test_migration.py:4）

## 必须修

1. **防止 CSV 公式注入。** 姓名、联系方式、奖惩标题和事由等文本直接交给 CSV writer，字段引用只处理 CSV 语法。电子表格可能把 `=cmd|` 或以 `+`、`-`、`@` 开头的文本解释为公式。两个导出共用的序列化入口应按字段类型对危险文本加单引号前缀，检查前导空白和控制字符，保留可信负分的数值形式。（app/profile.py:146、app/profile.py:164、app/profile.py:184、app/routes.py:322、app/routes.py:2650）
2. **对联系方式和奖惩事由做字段级授权。** 目前通用读取权限通过后，档案 HTML/JSON、个人 CSV 和奖惩列表直接给出敏感字段。按设计明确本人及获授权人员的字段权限，复用同一脱敏逻辑覆盖各入口，补同部门有档案读权限但无敏感字段权限的反例。（docs/设计说明书-v1.0.md:143、app/routes.py:2631、app/routes.py:2593、app/profile.py:167、app/profile.py:186）
3. **奖惩写入口先按部门查目标。** 获 `member:approve` 的部门管理员可从现役跨部的 403 与缺失、软删或离会的 40002 推断目标状态。持写锁按主键及部门取目标，跨部与不存在统一返回；通过范围校验后再判断本部门目标状态。档案及个人导出已有范围过滤，普通会员访问他人、部门管理员访问跨部或未知他人 ID 均为 403，全局读者查缺失为 404。（app/routes.py:278、app/routes.py:2602、app/routes.py:2648、app/routes.py:2693）
4. **复用 S4/S5 训练聚合。** 档案与 CSV 都从逐题明细取数，但分子全局按题去重，分母按任务题数相加。两个已指派任务各含同一道题且都解出时，档案为 50%，S4/S5 为 100%。复用按会员、任务聚合的 `progress_query`，再按所需范围汇总，零分母保留 0。考核记录已按学期及时间倒序。（app/profile.py:96、app/profile.py:124、app/profile.py:129、app/training.py:167、app/training.py:224、app/assessment.py:240）
5. **补统计组合维度及 HTTP 零值。** 四维均外连计数，在册排除退会、除名和软删，空库计数安全。HTTP 总走 scoped 分支，协会视图漏掉从未有会员的部门，单元测试测的是另一入口。TC-A-08 要求组合统计，目前只有四个单维。按角色选择部门取值域，补组合查询与明细对账；明确空维度和仅软删记录留下的取值如何展示。（app/profile.py:201、app/profile.py:226、app/profile.py:255、app/profile.py:278、app/routes.py:2742、tests/test_member_stats.py:53、docs/测试计划-v1.0.md:53）
6. **保留未录分与零分的区别。** 数据库允许分值为空，当前明细 CSV 把它导成 `0.00`。未录写空字符串，实际零分写 `0.00`，汇总仍将空值视为零。正负整数百分位转换正确，150 为 1.50，−50 为 −0.50。（docs/数据库设计-v1.0.md:176、app/profile.py:41、app/profile.py:62、app/profile.py:184）
7. **提供按学期汇总的综测导出。** 当前档案合计覆盖全部历史，导出仅逐条列奖惩，无法直接取得指定学期净分。保留历史档案，按发生日期和共用学期区间聚合，并提供明确的学期导出入口，覆盖边界日及正负抵扣。（docs/需求基线-v1.2.md:277、app/profile.py:46、app/profile.py:62、app/profile.py:184、app/assessment.py:38）
8. **修复烟测的节标题匹配。** 下载后确有内容检查，但 `grep -F` 会把模式中的反斜杠当作字面字符，合法 `[奖惩]` 会触发 `set -e` 提前退出。改用 CSV 解析后断言节标题、表头和奖励行的标题及 1.50 分，避免仅在任意位置命中文本。（scripts/smoke.sh:2、scripts/smoke.sh:405、app/profile.py:184）

## 其他核对结果

- 两种导出均有 UTF-8 BOM，记录用 CRLF，中文表头依次为基本信息、训练、考核、奖惩、赛事五节，列序与新增用例一致。赛事有五列表头及单格「无记录」，其余可空文本写空串。逐列文档契约、文件名及单格占位是否符合独立 spec 未核对，所查接口文档只规定 CSV，路线要求赛事空列。实际响应为 `text/csv; charset=utf-8`，附件分别为 `profile-学号-YYYYMMDD.csv` 和 `members-YYYYMMDD.csv`，个人非 CSV 返回 HTTP 400/40001。（app/profile.py:34、app/profile.py:146、app/profile.py:160、app/profile.py:297、app/routes.py:321、app/routes.py:2641、tests/test_profile_csv.py:38、docs/接口设计-v1.0.md:100、docs/开发路线-v1.0.md:82）
- 全量导出要求 `system:admin`，默认仅系统管理员满足，授权码也可放行。奖惩录入要求 `member:approve`，获授权部门管理员仍限本部门。统计直接访问检查 `member:read`，部门范围同时用于总数与分布，页面标注本部门和部门名。（app/auth.py:17、app/auth.py:60、app/routes.py:314、app/routes.py:1675、app/routes.py:2689、app/routes.py:2736、app/templates/member_stats.html:5）
- 数据库三列非空唯一索引配合 `BEGIN IMMEDIATE`。同键最多一笔写入，第二笔成功取锁后返回 40901 与原 ID，超时走 50301。此次为插入，无旧值覆盖的条件更新问题。0007 在新表建索引，标准 0006 库无历史重复，异常预建同名表会先在建表失败。降级删双索引及奖惩表，无配置残留，奖惩数据随表删除。（alembic/versions/0007_awards.py:17、alembic/versions/0007_awards.py:35、app/routes.py:1019、app/routes.py:2713、app/main.py:134）
- 未见新增 `except: pass`。常规应用日志只记录请求元数据及异常类型，未见分数或手机号正文；奖惩审计保存标题和分值，与业务写入同事务。档案逐类批查，统计按集合聚合，三个模板只读字典或已加载标量，未见逐记录延迟加载。（app/main.py:99、app/main.py:142、app/routes.py:2621、app/routes.py:2725、app/profile.py:252、app/templates/awards.html:14）

## 建议修

- 三页均发送 HTML Accept、断言 Content-Type 并查实际内容，分别覆盖学号和分值、在册数字、奖惩行文本。三条漏检反例：档案用例即使训练恒为零、考核行全丢仍通过；CSV 契约用例换成 LF 仍通过；统计 HTML 用例删除四张分布表仍通过。分别断言真实训练及考核数据、原始字节换行、分布表中的名称与计数。（tests/test_profile_pages.py:19、tests/test_profile_pages.py:33、tests/test_profile_pages.py:43、tests/test_profile.py:19、tests/test_profile_csv.py:27）
- 统计导航硬编码角色，显式获 `member:read` 的普通角色看不到入口；档案链接对已登录用户展示，奖惩和全量导出没有新增导航。按实际权限渲染，并补部门授权的直访正反例、全量导出成功例及双连接并发判重。（app/templates/base.html:13、app/templates/awards.html:6、tests/test_awards.py:32、tests/test_profile_csv.py:96）
- 分页缺失复发。奖惩列表与会员下拉全取，档案考核和奖惩历史全载入，全量导出逐会员查询并在内存组装。先按范围过滤再分页；批量导出按批聚合、流式写出。迁移测试可补 0006↔0007 往返及索引断言。（app/routes.py:317、app/routes.py:2667、app/routes.py:2679、app/profile.py:46、app/profile.py:129、tests/test_migration.py:4）
