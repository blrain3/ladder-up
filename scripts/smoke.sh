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

[[ $(json_field code) == 0 && $(json_field data db) == ok && $(json_field data migration) == 0003 ]]
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
grep -Fq "<dd>$student_no</dd>" "$work/body.json"

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

echo 'Smoke PASS: health 0003, admin login, demo login, anonymous apply, review, interview, admit, admitted member card 200, real PNG upload 200, fake PNG upload 400, password rotation'
