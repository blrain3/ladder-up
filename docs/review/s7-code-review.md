# S7 外部 OJ 同步与训练建议代码评审

**判定：修完再合并。** 评审对象为 `feat/s7-oj` 当前工作树（含未提交实现）。本次为静态评审，测试及烟测运行结果未核对。验收依据见 docs/开发路线-v1.0.md:84、docs/接口设计-v1.0.md:111、docs/接口设计-v1.0.md:205、docs/接口设计-v1.0.md:263、docs/数据库设计-v1.0.md:631、docs/测试计划-v1.0.md:77、docs/测试计划-v1.0.md:161、docs/adr/0003-会话认证与运行契约.md:39。

## 必须修（8 条）

1. **约束 HTTP 响应及读取时间。** 10 秒是套接字超时，慢速持续发送仍可拖住无上限的 `json.load`，深层 JSON 的 `RecursionError` 也会绕过重试。普通非 JSON 和部分字段缺失已有安全错误码，但缺状态、作者或题号仍可能入库，外部作者 handle 未验长度。加响应字节上限和请求总期限，验证状态、作者和关键字段，将解析失败归为固定错误码并补畸形响应测试。（app/oj.py:112、app/oj.py:172、app/oj.py:182）
2. **限制上传体与写锁占用。** 5 MiB 检查在 multipart 解析之后，只限制应用读入的文件。行数无上限，逐行解析与插入均持 SQLite 写锁，打卡可能等待至超时。接收时限制请求体，设物理行数上限，锁外完成文本解析，锁内批量核验绑定和写入。用独立连接在导入期间实际打卡，现有重试测试只在睡眠回调写配置。（app/auth.py:88、app/routes.py:2986、app/oj.py:270、tests/test_oj_retry.py:29、docs/开发路线-v1.0.md:88）
3. **收紧绑定和解绑范围。** CF 与 Vjudge 共用的正则允许点，CF 应只接受字母、数字、下划线和连字符，按平台落实长度上限并补临界测试。解绑指定 ID 或 handle 时先全平台查找，他人账号与不存在返回不同结果，可探测绑定。将所有条件并入本人删除语句并检查受影响行数，避免陈旧表单解绑新绑定。（app/oj.py:28、app/oj.py:63、app/routes.py:2890、app/templates/oj_accounts.html:38、tests/test_oj_bind.py:26）
4. **统一部门权限。** 建议页有部门限制，账号与提交页只判断 `member:read`，默认部门管理员可按 ID 读跨部账号，也可无筛选看全量提交。获得审批码的部门管理员还能触发全局同步及导入；作业详情默认也开放给部门训练读者。账号和列表应在查询中先限定部门，能影响全协会的作业与详情应限定全局授权，补双部门及显式授权反例。（app/auth.py:17、app/routes.py:2836、app/routes.py:2927、app/routes.py:3068、app/routes.py:3088、docs/review/s4-code-review.md:15）
5. **让同步失败可继续处理后续账号。** 拉取失败直接返回，后续账号本轮全部漏同步。此前账号已提交，最后错误码写入 `job_run.detail`，CLI 退出 1。应跳过失败账号继续，记录最后错误码及失败计数并保留失败状态。首次请求后确有三次重试，退避下限为 1/2/4 秒，与每两秒一请求叠加后典型间隔为 2/2/4。补中间账号失败后下一账号仍入库的用例。（app/oj.py:116、app/oj.py:322、bin/job.py:122、bin/job.py:200、tests/test_oj_retry.py:62、docs/接口设计-v1.0.md:267）
6. **恢复中断作业并交付调度入口。** OJ 固定空 `term`，三列索引对当前入口等效于作业名和上海业务日唯一。SQL 原子增加 `attempts`，运行中拒绝且不增加次数，HTTP 40902、CLI 退出 1。预留提交后 Web 进程退出会留下 `running`，同日无法恢复，跨午夜又可能同时执行。增加跨日互斥、过期接管和按本次运行标识条件结案，交付 ADR 约定的持久 timer 及补跑入口，仓库未找到 OJ timer，宿主机配置未核对。（alembic/versions/0006_assessment.py:75、bin/job.py:83、bin/job.py:127、app/routes.py:3049、docs/adr/0003-会话认证与运行契约.md:39）
7. **补齐 Vjudge 导入命令。** 路线要求 CLI，目前仅有 HTTP 导入，作业解析器只有考核、CF 同步和查询。复用同一受限导入入口，输出接受、拒绝、新增和跳过计数。接口文档还列有更新计数，应同步写明只插入的口径。（docs/开发路线-v1.0.md:86、docs/接口设计-v1.0.md:268、bin/job.py:171、app/routes.py:2998）
8. **让建议依据与文案一致。** 三条均从数据库现算并给出数值依据。缺卡按上海自然日含当天，周末计入，页面应标明。知识点取 14 天内最近三次测评，若要求最近三次有记录的测评，窗口之外的记录会被截掉。明确窗口及样本计数。完成率直接比较原比率，但两边保留两位小数可显示「60.00%，低于 60.00%」；改用题数和比较结论。无样本为空，零分母跳过，坏快照丢弃，缺失或非法阈值回退 0.6。（app/advice.py:58、app/advice.py:73、app/advice.py:85、app/assessment.py:74、app/assessment.py:132、app/templates/training_advice.html:13）

## 已核对与建议修

- **离线、回显与审计。** fixture 优先且读取失败不回退联网。无 fixture 且禁用时返回空结果，直接调用可为读取开关打开本地 SQLite。同步测试封住或替换 `urlopen`；烟测设可读 fixture、仅访问回环服务，断言接受和拒绝各 1、新增 3 及提交编号和 handle。CSV 依数据库绑定精确匹配，错误只回显行号和固定原因、最多 20 条，公式字段不回显。导入审计仅计数，绑定审计含 handle；作业日志无原始体，未见裸吞异常。（app/oj.py:144、app/oj.py:207、app/oj.py:261、app/templates/oj_import.html:14、app/routes.py:2908、app/routes.py:3004、bin/job.py:72、tests/test_oj_sync.py:13、tests/test_oj_retry.py:38、scripts/smoke.sh:23、scripts/smoke.sh:489）
- **数据库与列表。** 新空表建唯一索引无历史冲突，双绑定键含大小写不敏感约束。解绑外键置空，提交行保留但本人列表失去历史归属。提交去重由唯一键和原子冲突跳过保证，已断言重跑 0/3；建议补双连接并发和解绑历史用例。0007 无初始化键，0008 用登记表仅清理自建配置，预置同名键可保留。列表先过滤后分页，账号与题目一次联表，模板用已加载字段，无逐条查询；同步每账号重新读整张题号映射，可仅查该批题号。（alembic/versions/0007_awards.py:17、alembic/versions/0008_oj.py:18、alembic/versions/0008_oj.py:44、app/oj.py:235、app/oj.py:335、app/routes.py:2929、app/routes.py:2314、tests/test_oj_sync.py:17、tests/test_migration.py:143）
- **页面与三条漏检反例。** 六页均断言 HTML 类型与内容，覆盖 handle、AC 徽章、实际题名和建议分子分母；导入和同步 GET 主要查表单文案。删除 CSV 文件输入框仍可通过六页用例；将提交列表日期筛选整个删除仍可通过正向过滤样本；把导入计数审计改为空详情仍可通过仅查动作名的断言。分别断言表单控件、加入越界日期提交、比对审计计数。导入模板实际为 `oj_import.html`，导航按权限码显示。（tests/test_oj_pages.py:53、tests/test_oj_pages.py:70、tests/test_oj_pages.py:100、app/templates/oj_import.html:6、app/templates/oj_nav.html:1、app/templates/base.html:88、docs/review/s5-code-review.md:19、docs/review/s6-code-review.md:25）
