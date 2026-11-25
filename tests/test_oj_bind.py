import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import OjAccount
from app.oj import OjError, bind_account, unbind_account


def test_bind_persists_account_for_member(engine, accounts):
    with SessionLocal(bind=engine) as session, session.begin():
        account = bind_account(session, accounts[0]["student01"], "Codeforces", "Example-42")
        assert account.id and account.platform == "codeforces" and account.handle == "Example-42"


def test_binding_conflicts_for_member_platform_and_global_handle(engine, accounts):
    owner, other = accounts[0]["student01"], accounts[0]["student02"]
    with SessionLocal(bind=engine) as session, session.begin():
        bind_account(session, owner, "codeforces", "Example")
        for member, handle in ((owner, "different"), (other, "EXAMPLE")):
            with pytest.raises(OjError) as raised:
                bind_account(session, member, "codeforces", handle)
            assert raised.value.code == 40901
        bind_account(session, owner, "vjudge", "Example")


@pytest.mark.parametrize("handle", ["", "h aspace", "http://x", "账号"])
def test_invalid_handle(engine, accounts, handle):
    with SessionLocal(bind=engine) as session, pytest.raises(OjError) as raised:
        bind_account(session, accounts[0]["student01"], "codeforces", handle)
    assert raised.value.code == 40001


def test_unbind_removes_only_selected_member_binding(engine, accounts):
    owner = accounts[0]["student01"]
    other = accounts[0]["student02"]
    with SessionLocal(bind=engine) as session, session.begin():
        bind_account(session, owner, "vjudge", "owner")
        bind_account(session, other, "vjudge", "other")
        unbind_account(session, owner, "vjudge")
        assert session.scalars(select(OjAccount.handle)).all() == ["other"]


def test_account_routes_enforce_self_writes_and_allow_manager_reads(client, signed_in, coach, accounts):
    owner, other = accounts[0]["student01"], accounts[0]["student02"]
    headers = {"X-CSRF-Token": signed_in(client)}
    payload = {"platform": "codeforces", "handle": "my_handle"}
    response = client.post("/oj/accounts", json=payload, headers=headers)
    assert response.status_code == 200
    account = response.json()["data"]
    assert account["member_id"] == owner
    conflict = client.post("/oj/accounts", json=payload, headers=headers)
    assert conflict.status_code == 409 and conflict.json()["code"] == 40901
    invalid = client.post("/oj/accounts", json={**payload, "handle": "bad handle"}, headers=headers)
    assert invalid.status_code == 400 and invalid.json()["code"] == 40001
    assert client.get(f"/oj/accounts?member_id={other}").status_code == 403

    headers = {"X-CSRF-Token": signed_in(client, "coach0001")}
    assert client.get(f"/oj/accounts?member_id={owner}").json()["data"]["items"] == [account]
    for target in ({"account_id": account["id"]}, {"member_id": owner}, {"handle": "my_handle"}):
        response = client.post("/oj/accounts/unbind", json={"platform": "codeforces", **target}, headers=headers)
        assert response.status_code == 404

    headers = {"X-CSRF-Token": signed_in(client)}
    assert client.post("/oj/accounts/unbind", json={"platform": "codeforces"}, headers=headers).status_code == 200
    assert client.get("/oj/accounts").json()["data"]["items"] == []


def test_binding_requires_csrf(client, signed_in, engine):
    from app.models import AuditLog

    signed_in(client)
    response = client.post("/oj/accounts", json={"platform": "vjudge", "handle": "handle"})
    assert response.status_code == 400 and response.json()["code"] == 40002
    with SessionLocal(bind=engine) as session:
        assert session.scalar(select(OjAccount)) is None
        assert session.scalar(select(AuditLog).where(AuditLog.action == "oj.account.bound")) is None


@pytest.mark.parametrize(("platform", "character"), [("codeforces", "_"), ("vjudge", ".")])
def test_binding_handle_length_boundaries(engine, accounts, platform, character):
    with SessionLocal(bind=engine) as session, session.begin():
        owner = accounts[0]["student01"]
        assert bind_account(session, owner, platform, character * 80).handle == character * 80
        with pytest.raises(OjError, match="1–80"):
            bind_account(session, owner, platform, character * 81)
        if platform == "codeforces":
            with pytest.raises(OjError, match="1–80"):
                bind_account(session, owner, platform, "with.dot")


def test_cross_member_unbind_matches_missing_and_keeps_history(client, signed_in, engine, accounts):
    from app.models import OjSubmission
    from app.models.base import local_time

    with SessionLocal(bind=engine) as session, session.begin():
        other = bind_account(session, accounts[0]["student02"], "vjudge", "other")
        session.add(OjSubmission(
            platform="vjudge", external_id="historical", oj_account_id=other.id,
            verdict="AC", submitted_at=local_time(), synced_at=local_time(),
        ))
    headers = {"X-CSRF-Token": signed_in(client)}
    for selector in (
        {"account_id": other.id}, {"handle": "other"}, {"account_id": other.id + 1},
        {"member_id": other.member_id}, {"member_id": 999999},
    ):
        response = client.post("/oj/accounts/unbind", json={"platform": "vjudge", **selector}, headers=headers)
        assert response.status_code == 404
        assert response.json()["code"] == 40401
        assert response.json()["message"] == "要解绑的账号不存在"

    headers = {"X-CSRF-Token": signed_in(client, "student02")}
    assert client.get("/oj/submissions").json()["data"]["total"] == 1
    response = client.post("/oj/accounts/unbind", json={
        "platform": "vjudge", "account_id": other.id,
    }, headers=headers)
    assert response.status_code == 200
    assert client.get("/oj/submissions").json()["data"]["items"] == []
    with SessionLocal(bind=engine) as session:
        kept = session.scalar(select(OjSubmission))
        assert kept.external_id == "historical" and kept.oj_account_id is None
