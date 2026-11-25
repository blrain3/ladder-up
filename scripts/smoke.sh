#!/usr/bin/env bash
set -euo pipefail

python=$1
alembic=$2
work=$(mktemp -d "${TMPDIR:-/tmp}/ladder-smoke.XXXXXX" 2>/dev/null || mktemp -d /tmp/ladder-smoke.XXXXXX)
pid=
cleanup() {
    if [[ -n "$pid" ]]; then
        kill "$pid" 2>/dev/null || true
        wait "$pid" || true
    fi
    rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 1' INT TERM

export DATABASE_URL="sqlite:///$work/app.db"
export SEED_ADMIN_PASSWORD='SmokeAdminPassword!'
export SEED_DEMO_PASSWORD='Demo1234!'
export APP_ENV=development
export UPLOADS_DIR="$work/uploads"
export OJ_FIXTURE_PATH="$(pwd)/tests/fixtures/cf_user_status.json"
[[ -r "$OJ_FIXTURE_PATH" ]]

migration_head=$("$python" -c 'from alembic.config import Config
from alembic.script import ScriptDirectory
print(ScriptDirectory.from_config(Config("alembic.ini")).get_current_head())')
"$alembic" upgrade head
"$python" bin/seed.py

"$python" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >"$work/server.log" 2>&1 &
pid=$!

url=http://localhost:8000
http() {
    curl --noproxy '*' --resolve localhost:8000:127.0.0.1 --silent --show-error \
        --max-time 10 --output "$work/body.json" --write-out '%{http_code}' "$@"
}

for attempt in $(seq 1 50); do
    if ! kill -0 "$pid" 2>/dev/null; then break; fi
    if [[ $(http "$url/health" 2>/dev/null) == 200 ]]; then break; fi
    sleep 0.1
done
if ! kill -0 "$pid" 2>/dev/null || [[ $(http "$url/health") != 200 ]]; then
    cat "$work/server.log"
    exit 1
fi

expect_http() {
    local expected=$1 status
    shift
    status=$(http "$@")
    if [[ "$status" != "$expected" ]]; then
        printf 'Smoke FAIL: expected HTTP %s, got %s\n' "$expected" "$status" >&2
        exit 1
    fi
}

json_field() {
    "$python" -c '
import json, sys
with open(sys.argv[1]) as body:
    value = json.load(body)
for key in sys.argv[2:]:
    value = value[key]
if value is None:
    sys.exit("JSON field is null: " + ".".join(sys.argv[2:]))
print(value)
' "$work/body.json" "$@"
}

[[ $(json_field code) == 0 && $(json_field data db) == ok && $(json_field data migration) == "$migration_head" ]]
expect_http 303 -c "$work/admin.jar" -d student_no=admin0001 \
    --data-urlencode "password=$SEED_ADMIN_PASSWORD" "$url/auth/login"
expect_http 200 -b "$work/admin.jar" "$url/me"
[[ $(json_field data role) == sys_admin ]]
admin_id=$(json_field data id)
admin_csrf=$(json_field data csrf_token)

expect_http 303 -c "$work/member.jar" -d student_no=demo0001 \
    --data-urlencode "password=$SEED_DEMO_PASSWORD" "$url/auth/login"
expect_http 200 -b "$work/member.jar" "$url/me"
expect_http 403 -b "$work/member.jar" "$url/members/$admin_id"
[[ $(json_field code) == 40301 ]]

# Anonymous double-submit application flow.
expect_http 200 -c "$work/anon.jar" "$url/apply"
anon_csrf=$("$python" -c '
import re, sys
html = open(sys.argv[1], encoding="utf-8").read()
print(re.search(r"name=\"csrf_token\" value=\"([^\"]+)\"", html).group(1))
' "$work/body.json")
expect_http 200 -b "$work/anon.jar" -c "$work/anon.jar" \
    --data-urlencode "csrf_token=$anon_csrf" --data-urlencode "student_no=202400000001" \
    --data-urlencode "name=示例会员" --data-urlencode "agree_observation=1" "$url/apply"

app_id=$("$python" - <<'PY'
import os
from sqlalchemy import select

from app.db import SessionLocal, make_engine
from app.models import MemberApplication

engine = make_engine(os.environ["DATABASE_URL"])
with SessionLocal(bind=engine) as session:
    application = session.scalars(select(MemberApplication).where(
        MemberApplication.student_no == "202400000001",
        MemberApplication.apply_type == "入会",
        MemberApplication.active_flag == "1",
    )).one()
    print(application.id)
engine.dispose()
PY
)

expect_http 200 -b "$work/admin.jar" -H "X-CSRF-Token: $admin_csrf" \
    -H 'Content-Type: application/json' \
    -d '{"result":"通过","review_note":"材料齐全"}' \
    "$url/admin/applications/$app_id/review"
[[ $(json_field code) == 0 && $(json_field data status) == 待面试 ]]

expect_http 200 -b "$work/admin.jar" -H "X-CSRF-Token: $admin_csrf" \
    -H 'Content-Type: application/json' \
    -d '{"result":"通过","interview_note":"面试表现良好"}' \
    "$url/admin/applications/$app_id/interview"
[[ $(json_field code) == 0 && $(json_field data status) == 已面试 ]]

expect_http 200 -b "$work/admin.jar" -H "X-CSRF-Token: $admin_csrf" \
    -H 'Content-Type: application/json' -H 'Accept: text/html' -d '{}' \
    "$url/admin/applications/$app_id/admit"
temporary_password=$("$python" -c '
import re, sys
html = open(sys.argv[1], encoding="utf-8").read()
print(re.search(r"""id="one-time-password">([A-Za-z0-9_-]+)</strong>""", html).group(1))
' "$work/body.json")
read -r member_id student_no < <("$python" - "$app_id" <<'PYDB'
import os
import sys

from app.db import SessionLocal, make_engine
from app.models import Member, MemberApplication

engine = make_engine(os.environ["DATABASE_URL"])
with SessionLocal(bind=engine) as session:
    application = session.get(MemberApplication, int(sys.argv[1]))
    assert application.status == "已通过" and application.member_id is not None
    member = session.get(Member, application.member_id)
    assert member.student_no == application.student_no == "202400000001"
    assert member.member_status == "观察会员"
    print(member.id, member.student_no)
engine.dispose()
PYDB
)

expect_http 303 -c "$work/member2.jar" --data-urlencode "student_no=$student_no" \
    --data-urlencode "password=$temporary_password" "$url/auth/login"
expect_http 303 -b "$work/member2.jar" "$url/members/$member_id/card"
expect_http 200 -b "$work/member2.jar" "$url/me/password"
member_csrf=$("$python" -c '
import re, sys
html = open(sys.argv[1], encoding="utf-8").read()
print(re.search(r"""name="csrf_token" value="([^"]+)""", html).group(1))
' "$work/body.json")
expect_http 204 -b "$work/member2.jar" -c "$work/member2.jar" \
    --data-urlencode "csrf_token=$member_csrf" --data-urlencode "old_password=$temporary_password" \
    --data-urlencode 'new_password=SmokeNewMemberPassword!' "$url/me/password"
expect_http 200 -b "$work/member2.jar" "$url/me"
[[ $(json_field data id) == "$member_id" && $(json_field data student_no) == "$student_no" ]]
[[ $(json_field data member_status) == 观察会员 ]]
member_csrf=$(json_field data csrf_token)
expect_http 200 -b "$work/member2.jar" "$url/members/$member_id/card"
"$python" - "$work/body.json" "$student_no" <<'PYCARD'
import re
import sys
from pathlib import Path

html = Path(sys.argv[1]).read_text(encoding="utf-8")
details = re.search(r'<dl\b[^>]*\bid="member-card-details"[^>]*>(.*?)</dl>', html, re.S)
assert details, "会员证缺少信息容器"
student_no = re.search(r"<dt\b[^>]*>学号</dt>\s*<dd\b[^>]*>([^<]*)</dd>", details[1])
assert student_no and student_no[1].strip() == sys.argv[2], "会员证学号不匹配"
PYCARD

"$python" - "$work/photo.png" <<'PYPNG'
from base64 import b64decode
from pathlib import Path
import sys

Path(sys.argv[1]).write_bytes(b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFElEQVR4nGNknVbBgA0wYRUdtBIA20cBI1mXgH4AAAAASUVORK5CYII="
))
PYPNG
expect_http 200 -b "$work/member2.jar" -F "csrf_token=$member_csrf" \
    -F "file=@$work/photo.png;type=image/png" "$url/members/$member_id/photo"
attachment_id=$(json_field data attachmentId)
[[ "$attachment_id" =~ ^[1-9][0-9]*$ ]]
printf 'not an image\n' > "$work/fake.png"
expect_http 400 -b "$work/member2.jar" -F "csrf_token=$member_csrf" \
    -F "file=@$work/fake.png;type=image/png" "$url/members/$member_id/photo"
[[ $(json_field code) == 40001 ]]

# Password rotation still works after the application flow.
expect_http 303 -c "$work/demo2.jar" -d student_no=demo0002 \
    --data-urlencode "password=$SEED_DEMO_PASSWORD" "$url/auth/login"
expect_http 200 -b "$work/demo2.jar" "$url/me"
demo2_csrf=$(json_field data csrf_token)
cp "$work/demo2.jar" "$work/old-demo2.jar"
expect_http 204 -b "$work/demo2.jar" -c "$work/demo2.jar" -X PUT \
    --data-urlencode "csrf_token=$demo2_csrf" --data-urlencode "old_password=$SEED_DEMO_PASSWORD" \
    --data-urlencode 'new_password=SmokeReplacementPassword!' "$url/me/password"
expect_http 200 -b "$work/demo2.jar" "$url/me"
expect_http 401 -b "$work/old-demo2.jar" "$url/me"
[[ $(json_field code) == 40101 ]]

# S3 题库：教练登录 → 建父/子知识点 → 录题挂子知识点 → 父知识点检索命中 → 详情 XSS 转义 → 提交题解并渲染。
expect_http 303 -c "$work/coach.jar" -d student_no=demo0004 \
    --data-urlencode "password=$SEED_DEMO_PASSWORD" "$url/auth/login"
expect_http 200 -b "$work/coach.jar" "$url/me"
[[ $(json_field data role) == coach ]]
coach_csrf=$(json_field data csrf_token)

expect_http 200 -b "$work/coach.jar" -d "csrf_token=$coach_csrf" \
    --data-urlencode "name=图论" "$url/knowledge-points"
parent_id=$(json_field data id)
expect_http 200 -b "$work/coach.jar" -d "csrf_token=$coach_csrf" \
    --data-urlencode "name=最短路" --data-urlencode "parent_id=$parent_id" "$url/knowledge-points"
child_id=$(json_field data id)

expect_http 200 -b "$work/coach.jar" -d "csrf_token=$coach_csrf" \
    --data-urlencode "title=最短路入门" --data-urlencode "difficulty=入门" \
    --data-urlencode "problem_type=程序" --data-urlencode "source=自建" \
    --data-urlencode "kp_ids=$child_id" \
    --data-urlencode "sample_in=1 2" --data-urlencode "sample_out=3" \
    --data-urlencode "statement=<script>alert(1)</script> <img src=x onerror=alert(1)> [x](javascript:alert(1))" "$url/problems"
problem_id=$(json_field data id)

expect_http 200 -b "$work/coach.jar" "$url/problems?kp=$parent_id"
[[ $(json_field data total) == 1 ]]
"$python" - "$work/body.json" "$problem_id" <<'PYPROB'
import json, sys
with open(sys.argv[1], encoding="utf-8") as body:
    data = json.load(body)
assert data["data"]["items"][0]["id"] == int(sys.argv[2]), data
PYPROB

expect_http 200 -b "$work/coach.jar" -H 'Accept: text/html' "$url/problems/$problem_id"
"$python" - "$work/body.json" <<'PYXSS'
import re
import sys
from html.parser import HTMLParser
from pathlib import Path


class StatementParser(HTMLParser):
    def handle_starttag(self, tag, attrs):
        assert not tag.startswith("script"), "题面含未转义的 script 标签"
        for name, value in attrs:
            assert name != "onerror", "题面含 onerror 属性"
            assert "javascript:" not in (value or "").lower(), "题面含 javascript: 属性值"


html = Path(sys.argv[1]).read_text(encoding="utf-8")
statement = re.search(r'<article\b[^>]*\bid="problem-statement"[^>]*>(.*?)</article>', html, re.S)
assert statement, "缺少题面容器"
assert "&lt;script&gt;" in statement[1]
assert "&lt;img src=x onerror=alert(1)&gt;" in statement[1]
StatementParser().feed(statement[1])
PYXSS

expect_http 200 -b "$work/coach.jar" -d "csrf_token=$coach_csrf" \
    --data-urlencode 'content_md=**题解** [x](javascript:alert(1))' \
    "$url/problems/$problem_id/solutions"
[[ $(json_field code) == 0 ]]
expect_http 200 -b "$work/coach.jar" -H 'Accept: text/html' "$url/problems/$problem_id"
grep -Fq '<strong>题解</strong>' "$work/body.json"
if grep -Fq 'javascript:' "$work/body.json"; then
    printf 'Smoke FAIL: javascript link was not removed\n' >&2
    exit 1
fi

read -r term phase_start phase_end < <("$python" - <<'PYTERM'
import os
from app.assessment import current_term, term_interval
from app.db import SessionLocal, make_engine
with SessionLocal(bind=make_engine(os.environ["DATABASE_URL"])) as session:
    term = current_term(session)
    print(term, *term_interval(term))
PYTERM
)

# S4 训练：计划、阶段、冻结指派、逐题打卡、进度、拒绝访问与原行补卡。
expect_http 200 -b "$work/coach.jar" -d "csrf_token=$coach_csrf" -d "term=$term" "$url/training-plans"
plan_id=$(json_field data id)
[[ "$plan_id" =~ ^[1-9][0-9]*$ && $(json_field data term) == "$term" ]]
expect_http 200 -b "$work/coach.jar" -d "csrf_token=$coach_csrf" -d "plan_id=$plan_id" \
    --data-urlencode 'name=训练烟测阶段' -d "start_on=$phase_start" -d "end_on=$phase_end" "$url/training-phases"
phase_id=$(json_field data id)
[[ "$phase_id" =~ ^[1-9][0-9]*$ && $(json_field data name) == 训练烟测阶段 ]]

expect_http 200 -b "$work/coach.jar" -d "csrf_token=$coach_csrf" \
    --data-urlencode 'title=训练烟测第二题' --data-urlencode 'difficulty=入门' \
    --data-urlencode 'problem_type=程序' --data-urlencode 'source=自建' \
    -d sample_in=1 -d sample_out=1 -d "kp_ids=$child_id" "$url/problems"
second_problem=$(json_field data id)
[[ "$second_problem" =~ ^[1-9][0-9]*$ && "$second_problem" != "$problem_id" ]]
expect_http 200 -b "$work/coach.jar" -d "csrf_token=$coach_csrf" -d "phase_id=$phase_id" \
    --data-urlencode 'title=训练烟测任务' --data-urlencode 'assignee_type=指定会员' \
    -d "problem_ids=$problem_id" -d "problem_ids=$second_problem" "$url/training-tasks"
task_id=$(json_field data id)
[[ "$task_id" =~ ^[1-9][0-9]*$ && $(json_field data title) == 训练烟测任务 ]]
expect_http 200 -b "$work/member.jar" "$url/me"
demo1_id=$(json_field data id)
demo1_csrf=$(json_field data csrf_token)
expect_http 200 -b "$work/coach.jar" -d "csrf_token=$coach_csrf" \
    --data-urlencode 'assignee_type=指定会员' -d "member_ids=$demo1_id" "$url/training-tasks/$task_id/assign"
[[ $(json_field data status) == 已下发 && $(json_field data assignees) == 1 ]]
expect_http 200 -b "$work/coach.jar" "$url/training-tasks/$task_id"
"$python" - "$work/body.json" "$demo1_id" "$problem_id" "$second_problem" <<'PYTASK'
import json, sys
with open(sys.argv[1], encoding="utf-8") as body:
    task = json.load(body)["data"]
assert task["published_at"] and [row["id"] for row in task["assignees"]] == [int(sys.argv[2])]
assert [row["problem_id"] for row in task["problems"]] == list(map(int, sys.argv[3:]))
PYTASK

today=$(TZ=Asia/Shanghai date +%F)
expect_http 200 -b "$work/member.jar" -d "csrf_token=$demo1_csrf" -d "task_id=$task_id" \
    -d "checkin_date=$today" -d solved_count=1 -d "solved_problem_ids=$problem_id" "$url/checkins"
checkin_id=$(json_field data id)
[[ "$checkin_id" =~ ^[1-9][0-9]*$ && $(json_field data solved_count) == 1 ]]
expect_http 200 -b "$work/member.jar" -H 'Accept: text/html' "$url/training-progress?phase_id=$phase_id"
grep -Fq '1 / 2' "$work/body.json"
expect_http 404 -b "$work/demo2.jar" "$url/training-tasks/$task_id"
[[ $(json_field code) == 40401 ]]

count_checkins() {
    "$python" -c '
import os, sqlite3
with sqlite3.connect(os.environ["DATABASE_URL"].removeprefix("sqlite:///")) as connection:
    print(connection.execute("SELECT count(*) FROM checkin").fetchone()[0])
'
}

expect_http 200 -b "$work/coach.jar" "$url/checkins?task_id=$task_id"
before=$(count_checkins)
[[ "$before" == 1 && $(json_field data total) == "$before" ]]
expect_http 200 -b "$work/coach.jar" -d "csrf_token=$coach_csrf" -d "task_id=$task_id" \
    -d "member_id=$demo1_id" -d "checkin_date=$today" --data-urlencode 'note=烟测补卡' "$url/checkins/makeup"
[[ $(json_field data id) == "$checkin_id" && $(json_field data created) == False && $(json_field data status) == 补卡 ]]
expect_http 200 -b "$work/coach.jar" "$url/checkins?task_id=$task_id"
[[ $(count_checkins) == "$before" && $(json_field data total) == "$before" ]]
"$python" - "$work/body.json" "$checkin_id" "$problem_id" "$second_problem" <<'PYCHECKIN'
import json, os, sqlite3, sys
with open(sys.argv[1], encoding="utf-8") as body:
    row, = json.load(body)["data"]["items"]
assert row["id"] == int(sys.argv[2]) and row["note"] == "烟测补卡"
assert {item["problem_id"]: item["solved"] for item in row["problems"]} == {
    int(sys.argv[3]): 1, int(sys.argv[4]): 0,
}
with sqlite3.connect(os.environ["DATABASE_URL"].removeprefix("sqlite:///")) as connection:
    audit, = connection.execute(
        "SELECT after_json FROM audit_log WHERE action = 'checkin.makeup' AND target_id = ?", (row["id"],)
    ).fetchall()
assert json.loads(audit[0])["note"] == "烟测补卡"
PYCHECKIN

# S5：补一题已完成的训练，令 demo0001 的进度达到 2/3。
expect_http 200 -b "$work/coach.jar" -d "csrf_token=$coach_csrf" -d "phase_id=$phase_id" \
    --data-urlencode 'title=转正烟测任务' -d "problem_ids=$problem_id" "$url/training-tasks"
extra_task=$(json_field data id)
[[ "$extra_task" =~ ^[1-9][0-9]*$ && $(json_field data title) == 转正烟测任务 ]]
expect_http 200 -b "$work/coach.jar" -d "csrf_token=$coach_csrf" \
    --data-urlencode 'assignee_type=指定会员' -d "member_ids=$demo1_id" "$url/training-tasks/$extra_task/assign"
[[ $(json_field data status) == 已下发 && $(json_field data assignees) == 1 ]]
expect_http 200 -b "$work/member.jar" -d "csrf_token=$demo1_csrf" -d "task_id=$extra_task" \
    -d "checkin_date=$today" -d solved_count=1 -d "solved_problem_ids=$problem_id" "$url/checkins"
[[ $(json_field data solved_count) == 1 ]]

expect_http 200 -b "$work/coach.jar" -d "csrf_token=$coach_csrf" \
    --data-urlencode 'title=S5 烟测周测' --data-urlencode 'test_type=周测' \
    -d "held_on=$phase_start" "$url/assessment-tests"
test_id=$(json_field data id)
[[ "$test_id" =~ ^[1-9][0-9]*$ && $(json_field data title) == 'S5 烟测周测' ]]
expect_http 200 -b "$work/demo2.jar" "$url/me"
demo2_id=$(json_field data id)
demo2_csrf=$(json_field data csrf_token)
demo2_status=$(json_field data member_status)
[[ $(json_field data student_no) == demo0002 && "$demo2_status" == 正式会员 ]]
expect_http 200 -b "$work/coach.jar" -H "X-CSRF-Token: $coach_csrf" -H 'Content-Type: application/json' \
    -d "{\"rows\":[{\"member_id\":$demo1_id,\"version\":0,\"solved_count\":2,\"score\":90},{\"member_id\":$demo2_id,\"version\":0,\"solved_count\":0,\"score\":50}]}" \
    "$url/assessment-tests/$test_id/scores"
[[ $(json_field data created) == 2 && $(json_field data updated) == 0 ]]
expect_http 200 -b "$work/coach.jar" "$url/assessment-tests/$test_id/scores"
"$python" - "$work/body.json" "$demo1_id" "$demo2_id" <<'PYSCORES'
import json, sys
with open(sys.argv[1], encoding="utf-8") as body:
    rows = {row["id"]: row for row in json.load(body)["data"]["items"]}
assert rows[int(sys.argv[2])]["score"] == "90.00"
assert rows[int(sys.argv[3])]["solved_count"] == 0
PYSCORES

expect_http 200 -b "$work/admin.jar" -d "csrf_token=$admin_csrf" -d "term=$term" "$url/assessments/run"
[[ $(json_field data passed) == 1 && $(json_field data created) -ge 2 ]]
expect_http 200 -b "$work/admin.jar" "$url/promotions?term=$term"
"$python" - "$work/body.json" "$demo1_id" <<'PYPROMOTE'
import json, sys
with open(sys.argv[1], encoding="utf-8") as body:
    row, = (row for row in json.load(body)["data"]["items"] if row["id"] == int(sys.argv[2]))
assert row["student_no"] == "demo0001" and row["ready"] and row["assessment_result"] == "通过"
assert (row["completion_solved"], row["completion_total"]) == (2, 3)
PYPROMOTE
expect_http 200 -b "$work/admin.jar" -d "csrf_token=$admin_csrf" -d "term=$term" "$url/members/$demo1_id/promote"
[[ $(json_field data member_status) == 正式会员 ]]
expect_http 200 -b "$work/member.jar" "$url/me"
[[ $(json_field data member_status) == 正式会员 ]]

# S6 档案与统计：教练给 demo0001 录一条奖励（+150）→ 档案页显示奖惩与分值 →
# 下载 CSV 含「奖惩」节与标题 → 统计页在册人数 ≥5。
expect_http 200 -b "$work/coach.jar" -H "X-CSRF-Token: $coach_csrf" -H 'Content-Type: application/json' \
    -d "{\"member_id\":$demo1_id,\"award_type\":\"奖励\",\"title\":\"烟测奖励\",\"happened_on\":\"$today\",\"extra_points\":150}" \
    "$url/awards"
[[ $(json_field data extra_points) == 150 && $(json_field data points) == 1.50 ]]

expect_http 200 -b "$work/member.jar" -H 'Accept: text/html' "$url/members/$demo1_id/profile"
grep -Fq '烟测奖励' "$work/body.json"
grep -Fq '1.50' "$work/body.json"

expect_http 200 -b "$work/member.jar" "$url/members/$demo1_id/export?format=csv"
"$python" - "$work/body.json" <<'PYPROFILE'
import codecs
import csv
import io
import sys
from pathlib import Path

body = Path(sys.argv[1]).read_bytes()
assert body.startswith(codecs.BOM_UTF8)
rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"), newline="")))
sections = [row[0] for row in rows if len(row) == 1 and row[0].startswith("[")]
assert sections == ["[基本信息]", "[训练]", "[考核]", "[奖惩]", "[赛事]", "[综测汇总]"]
start, end = rows.index(["[奖惩]"]), rows.index(["[赛事]"])
assert rows[start + 1] == ["日期", "类型", "标题", "分值(分)", "事由"]
assert any(row[1:4] == ["奖励", "烟测奖励", "1.50"] for row in rows[start + 2:end])
assert rows[-1] == ["未指定学期", "", "", ""]
PYPROFILE

expect_http 200 -b "$work/coach.jar" "$url/stats/members"
[[ $(json_field data active) -ge 5 ]]

expect_http 200 -b "$work/demo2.jar" -d "csrf_token=$demo2_csrf" "$url/members/$demo2_id/leave"
[[ $(json_field data status) == 待审核 && $(json_field data member_status) == "$demo2_status" ]]
expect_http 200 -b "$work/demo2.jar" "$url/me"
[[ $(json_field data member_status) == "$demo2_status" ]]
expect_http 200 -b "$work/admin.jar" -d "csrf_token=$admin_csrf" "$url/members/$demo2_id/leave/approve"
[[ $(json_field data status) == 已通过 && $(json_field data member_status) == 已退会 ]]
expect_http 200 -b "$work/admin.jar" "$url/members/$demo2_id"
[[ $(json_field data member_status) == 已退会 ]]
expect_http 401 -b "$work/demo2.jar" "$url/me"
[[ $(json_field code) == 40101 ]]

expect_http 303 -c "$work/demo3.jar" -d student_no=demo0003 \
    --data-urlencode "password=$SEED_DEMO_PASSWORD" "$url/auth/login"
expect_http 200 -b "$work/demo3.jar" "$url/me"
demo3_id=$(json_field data id)
[[ $(json_field data student_no) == demo0003 ]]
expect_http 200 -b "$work/admin.jar" -d "csrf_token=$admin_csrf" \
    --data-urlencode 'reason=烟测确认会员多次违反协会管理规定' "$url/members/$demo3_id/expel"
[[ $(json_field data member_status) == 已除名 ]]
expect_http 200 -b "$work/admin.jar" "$url/members/$demo3_id"
[[ $(json_field data member_status) == 已除名 ]]
expect_http 401 -b "$work/demo3.jar" "$url/me"
[[ $(json_field code) == 40101 ]]
expect_http 401 -d student_no=demo0003 \
    --data-urlencode "password=$SEED_DEMO_PASSWORD" "$url/auth/login"
[[ $(json_field code) == 40101 ]]

# S7: replay Vjudge CSV and the local Codeforces response, then recheck the training page.
expect_http 200 -b "$work/member.jar" -d "csrf_token=$demo1_csrf" \
    -d platform=vjudge -d handle=ladder_csv "$url/oj/accounts"
expect_http 200 -b "$work/member.jar" -d "csrf_token=$demo1_csrf" \
    -d platform=codeforces -d handle=ladder_smoke "$url/oj/accounts"
cat > "$work/vjudge.csv" <<'CSV'
handle,problem,verdict,submitted_at
ladder_csv,HDU-1000,AC,2026-11-24 10:00:00
ladder_csv,HDU-1000,INVALID,2026-11-24 10:01:00
CSV
expect_http 200 -b "$work/coach.jar" -F "csrf_token=$coach_csrf" \
    -F "file=@$work/vjudge.csv;type=text/csv" "$url/oj/vjudge-import"
[[ $(json_field data accepted) == 1 && $(json_field data rejected) == 1 ]]
expect_http 202 -b "$work/coach.jar" -d "csrf_token=$coach_csrf" \
    -d platform=codeforces "$url/oj/sync"
job_run_id=$(json_field data jobRunId)
[[ "$job_run_id" =~ ^[1-9][0-9]*$ ]]
for attempt in $(seq 1 30); do
    expect_http 200 -b "$work/coach.jar" "$url/jobs/$job_run_id"
    job_status=$(json_field data status)
    if [[ "$job_status" != running ]]; then break; fi
    sleep 0.1
done
[[ "$job_status" == success && $(json_field data detail created) == 3 ]]
expect_http 200 -b "$work/member.jar" -H 'Accept: text/html' "$url/oj/submissions?platform=codeforces"
grep -Fq '770003' "$work/body.json"
grep -Fq 'ladder_smoke' "$work/body.json"
expect_http 200 -b "$work/member.jar" -H 'Accept: text/html' "$url/checkins?task_id=$task_id"
grep -Fq '逐题打卡' "$work/body.json"
grep -Fq '>补卡</span>' "$work/body.json"

echo "Smoke PASS: health $migration_head, admin login, demo login, anonymous apply, review, interview, admit, admitted member card 200, real PNG upload 200, fake PNG upload 400, password rotation, coach problem bank flow (kp tree, search by parent, XSS escaped, solution rendered), training flow, S5 assessment and membership flow, S6 awards/profile CSV/member stats, S7 offline OJ imports/sync and checkin page"
