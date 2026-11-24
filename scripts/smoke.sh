#!/usr/bin/env bash
set -euo pipefail

python=$1
alembic=$2
work=$(mktemp -d)
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
export SMOKE_MEMBER_PASSWORD='SmokeMemberPassword!'
export APP_ENV=development

"$alembic" upgrade head
"$python" bin/seed.py
# The real seed intentionally generates undisclosed passwords for ordinary members.
# Set a known credential only inside this disposable database.
"$python" - <<'PY'
import os

from sqlalchemy import select

from app.db import SessionLocal, make_engine
from app.models import Member
from app.security import hash_password

engine = make_engine(os.environ["DATABASE_URL"])
with SessionLocal(bind=engine) as session, session.begin():
    member = session.scalars(select(Member).where(Member.student_no == "demo0001")).one()
    member.password_hash = hash_password(os.environ["SMOKE_MEMBER_PASSWORD"])
engine.dispose()
PY

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
print(value)
' "$work/body.json" "$@"
}

[[ $(json_field code) == 0 && $(json_field data db) == ok && $(json_field data migration) == 0002 ]]
expect_http 303 -c "$work/admin.jar" -d student_no=admin0001 \
    --data-urlencode "password=$SEED_ADMIN_PASSWORD" "$url/auth/login"
expect_http 200 -b "$work/admin.jar" "$url/me"
[[ $(json_field data role) == sys_admin ]]
admin_id=$(json_field data id)

expect_http 303 -c "$work/member.jar" -d student_no=demo0001 \
    --data-urlencode "password=$SMOKE_MEMBER_PASSWORD" "$url/auth/login"
expect_http 403 -b "$work/member.jar" "$url/members/$admin_id"
[[ $(json_field code) == 40301 ]]

expect_http 200 -b "$work/member.jar" "$url/me"
csrf=$(json_field data csrf_token)
cp "$work/member.jar" "$work/old-member.jar"
expect_http 204 -b "$work/member.jar" -c "$work/member.jar" -X PUT \
    --data-urlencode "csrf_token=$csrf" --data-urlencode "old_password=$SMOKE_MEMBER_PASSWORD" \
    --data-urlencode 'new_password=SmokeReplacementPassword!' "$url/me/password"
expect_http 200 -b "$work/member.jar" "$url/me"
expect_http 401 -b "$work/old-member.jar" "$url/me"
[[ $(json_field code) == 40101 ]]

echo 'Smoke PASS: GET /health 200, admin login 303, member login 303, out-of-scope detail 40301, password change 204, old Cookie 40101'
