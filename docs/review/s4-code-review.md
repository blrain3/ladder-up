# S4 训练切片代码评审

**判定：修完再合并。必须修 9 条。** 审查对象为 `feat/s4-training` 当前工作树，含未跟踪实现。验收依据为 `docs/开发路线-v1.0.md:66`、`docs/接口设计-v1.0.md:124`、`docs/数据库设计-v1.0.md:273`、`docs/数据库设计-v1.0.md:611`、`docs/测试计划-v1.0.md:70`、`docs/测试计划-v1.0.md:160`、`docs/测试计划-v1.0.md:191` 和 `docs/设计说明书-v1.0.md:123`。

隔离目录全量复跑未复现预警失败。唯一失败为 S2 测试依赖相对迁移路径，补齐路径后单跑通过（`tests/test_migration.py:26`、`tests/test_training_alerts.py:23`）。

## 必须修

1. **冻结队伍成员并对齐下发接口。** 全体按下发时的有效会员展开，指定会员沿用草稿名单，指派行和 `published_at/status` 同事务写入。测试在下发后直接插入会员，断言指派表无该人、可见性函数拒绝，未走新会员列表或详情 HTTP。指定队伍却保存部门 ID，列表、详情和预警按当前部门匹配，后来入部的人会看到旧任务。队伍表在 E 域后置。若本期沿用部门代理，先明确界面和接口语义，下发时固化对应会员 ID。接入正式队伍后从其成员关系展开。让文档约定的 `/assign` 执行下发，草稿编辑另设入口。（`app/training.py:43`、`app/training.py:76`、`app/training.py:87`、`app/training.py:180`、`app/training.py:254`、`app/routes.py:1830`、`app/routes.py:1848`、`tests/test_training_publish.py:16`、`docs/数据库设计-v1.0.md:72`、`docs/接口设计-v1.0.md:128`）
2. **以唯一键判重，恢复写锁超时契约。** 三列非空且有唯一索引。当前在 `BEGIN IMMEDIATE` 后先查再写，两笔同键、均通过截止检查且成功取锁时，一笔成功，另一笔查到原 ID 并返回 40901。实际撞唯一键走通用处理，缺原 ID。改为插入并仅捕获该键冲突，回滚后查 ID。锁超时目前返回题库 40901，应交给全局处理返回 50301，补独立连接同键并发及 NF-05 的百行导入并发打卡测试。现有锁测试只写部门。创建任务、按顺序写多选题和审计同事务，异常关闭会话会回滚。下发和普通打卡共用写锁，普通打卡拿锁后查指派，先打卡会拒绝草稿，先下发则按已提交名单判断。（`alembic/versions/0005_training.py:76`、`app/routes.py:966`、`app/routes.py:1481`、`app/routes.py:1777`、`app/routes.py:1974`、`app/db.py:25`、`app/db.py:40`、`app/main.py:134`、`tests/test_db.py:43`、`tests/test_checkin_rules.py:13`）
3. **补卡校验已下发且已指派。** 无记录建行，有则更新原行状态及所传备注，唯一索引防止第二行，成功写 `checkin.makeup` 审计。不存在的任务返回 404，无效会员返回 400，有效未指派会员或草稿仍可写。持锁检查发布状态及目标会员的冻结指派，拒绝时零业务写入。（`app/routes.py:1551`、`app/routes.py:2014`、`app/training.py:277`、`alembic/versions/0005_training.py:92`）
4. **校验逐题归属并按题去重。** 进度依赖逐题明细，缓存计数不一致会标记并展示复算值，符合文档。软删从分母剔除，已解却留在分子，且同题跨日重复累加。JSON 还可提交包外存在的题，表单则静默忽略非法题号。提交时统一校验有效题包及题号唯一，统计时按任务当前有效题包去重计已解，保留历史打卡明细。（`app/routes.py:1934`、`app/routes.py:1991`、`app/training.py:105`、`app/training.py:115`、`app/training.py:134`、`app/templates/task_detail.html:103`、`docs/接口设计-v1.0.md:230`）
5. **保证计划区间可校验。** 同计划闭区间用 `<=` 与 `>=` 判交集，共享边界日也拒绝。阶段仅持一个计划外键，任务仅持一个阶段外键，没有双计划字段可互相误挂。计划接受任意学期字符串，未知格式跳过区间校验，年份 0000 会抛异常。创建时验证学期、年份及学年顺序，明确假期边界并处理已有不可识别计划。（`app/routes.py:1441`、`app/routes.py:1672`、`app/routes.py:1711`、`app/routes.py:1778`、`alembic/versions/0005_training.py:28`）
6. **普通打卡统一截止规则。** 日期型截止补到上海当地 23:59:59，但当前带微秒比较，最后一秒内也可能拒绝。教练权限直接绕过普通打卡截止，测试还要求成功。按秒比较完整截止秒，普通打卡对所有角色同样拒绝，超期走补卡。普通入口接受客户端传入过去或未来的日期，日期窗口应与补卡口径一并明确。（`app/routes.py:1508`、`app/routes.py:1526`、`app/routes.py:1979`、`tests/test_checkin_rules.py:31`）
7. **落实资源范围与个人信息可见性。** 权限码上，会员有本人读与打卡码，部门管理员有读写，教练还可审批，系统管理员通配。普通打卡只验登录和指派，未检查本人写码。部门读写实际上全局，会员详情 JSON 还回传其他受派人的姓名学号。按管辖范围授权并裁剪响应。未指派详情实际 403，未知 ID 404，可区分存在性。建议会员读取统一 404，保留拒绝审计，符合 S06 和权限防泄漏要求。（`app/auth.py:17`、`app/auth.py:60`、`app/routes.py:1423`、`app/routes.py:1573`、`app/routes.py:1622`、`app/routes.py:1807`、`app/routes.py:1970`、`docs/设计说明书-v1.0.md:143`）
8. **修测试，保留预警双条件。** 当前重写版本单跑通过，原失败现场未核对。阈值来自 `sys_config`，无效值回退 0.6，零题分母安全且只可因缺卡报警。缺卡按上海自然日含首尾和当天，周末照计，未按工作日历。低于阈值与缺卡至少两天各自触发，不能仅凭达标断言无预警。固定上海日期，设下发日和打卡日相同，分别验证 0.5、恰好 0.6、0.8，并将缺卡测试设阈值 0、断言恰好两天。（`app/routes.py:1631`、`app/training.py:155`、`app/training.py:219`、`app/training.py:261`、`tests/test_training_alerts.py:23`、`tests/test_training_alerts.py:60`）
9. **补全迁移回滚。** 0005 新建任务表，0004 及之前无任务可回填。新任务 `published_at=NULL/status=草稿` 合理。降级按依赖逆序拆表，阈值配置仍残留。使迁移能识别自己插入的配置并在降级移除，保留升级前已有的同名配置。（`alembic/versions/0005_training.py:39`、`alembic/versions/0005_training.py:105`、`alembic/versions/0005_training.py:116`）

## 建议修

- 六个指定页面都发了 HTML Accept，也都查文本，另外覆盖阶段页。补响应 Content-Type、实际数据及角色视图，尤其打卡页的题目名与提交按钮、详情的任务标题、进度的 1/2、预警的会员及缺卡数。（`tests/test_training_pages.py:4`、`tests/test_training_pages.py:27`、`app/templates/checkin.html:15`、`app/templates/training_progress.html:57`、`app/templates/training_alerts.html:24`）
- 三处可漏回归。新建补卡丢掉备注和成功审计仍通过，只查状态。进度改读缓存计数仍通过，夹具两值相等，应直接改缓存再复算。缺卡把两天门槛改成三天仍通过，夹具含今天共三天，且低完成率独立命中。（`tests/test_makeup.py:17`、`tests/test_training_progress.py:17`、`tests/test_training_alerts.py:60`）
- 烟测止于题库，无训练追加段，所列下发、1/2 打卡、未指派 403、补卡均未覆盖，训练步骤也无仅凭 200 放行的情况。补端到端链路，检查指派集合、逐题与进度、拒绝语义、补卡 ID/备注和审计。（`scripts/smoke.sh:249`、`scripts/smoke.sh:259`）
- 任务列表逐项查题量和指派量，无逐项打卡计数查询。进度逐任务查打卡再逐条取明细，预警逐任务扫描会员。按可见范围分页，批量聚合，补任务截止筛选和打卡任务、人、日期筛选，当前 `task_id` 仅切表单，记录固定前 50 条。（`app/routes.py:1606`、`app/routes.py:1745`、`app/routes.py:1897`、`app/training.py:134`、`app/training.py:253`）
- 切片未见 `try/except: pass`。请求日志记录路径和会员 ID，异常日志记录类型及请求 ID，未见打印令牌或请求体。模板直传计划、阶段、题目、会员等 ORM 对象，当前读取已载入标量，未见延迟加载异常。任务创建表单对会员也展示，建议按写权限加载选项并渲染。（`app/main.py:99`、`app/main.py:142`、`app/routes.py:1700`、`app/routes.py:1762`、`app/routes.py:1817`、`app/templates/training_tasks.html:6`、`app/templates/task_detail.html:39`）
