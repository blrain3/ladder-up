import json

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import AuditLog, TestScore


def _score_row(member_id, *, solved=3, score=80):
    return {"member_id": member_id, "solved_count": solved, "score": score}


def test_first_score_creates_row_and_audit(assessment_api):
    api = assessment_api
    test_id = api["create_test"]()
    result = api["record"](test_id, [_score_row(api["ids"]["student01"])])
    assert result == {"created": 1, "updated": 0}

    with SessionLocal(bind=api["engine"]) as session:
        score = session.scalar(select(TestScore).where(TestScore.test_id == test_id))
        assert score is not None
        assert score.member_id == api["ids"]["student01"]
        assert score.score == "80.00"
        created = session.scalar(select(AuditLog).where(AuditLog.action == "score.created"))
        assert created is not None
        assert json.loads(created.after_json)["score"] == "80.00"


def test_repeat_score_updates_in_place_with_old_value_audit(assessment_api):
    api = assessment_api
    test_id = api["create_test"]()
    api["record"](test_id, [_score_row(api["ids"]["student01"], solved=1, score=60)])

    result = api["record"](test_id, [_score_row(api["ids"]["student01"], solved=4, score=75)])
    assert result == {"created": 0, "updated": 1}

    with SessionLocal(bind=api["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(TestScore).where(
            TestScore.test_id == test_id
        )) == 1
        score = session.scalar(select(TestScore).where(TestScore.test_id == test_id))
        assert score.score == "75.00" and score.solved_count == 4
        updated = session.scalar(select(AuditLog).where(AuditLog.action == "score.updated"))
        assert updated is not None
        assert json.loads(updated.before_json) == {"solved_count": 1, "score": "60.00"}
        after = json.loads(updated.after_json)
        assert after["updated"] is True and after["score"] == "75.00"


def test_invalid_score_fields_return_40001_without_writes(assessment_api):
    api = assessment_api
    test_id = api["create_test"]()
    csrf = api["member_csrf"]("coach0001")
    for payload in (
        {"rows": [_score_row(api["ids"]["student01"], solved=-1)]},
        {"rows": [_score_row(api["ids"]["student01"], solved=2**31)]},
        {"rows": [_score_row(api["ids"]["student01"], score=-0.01)]},
        {"rows": [_score_row(api["ids"]["student01"], score=10000)]},
        {"rows": [{"member_id": api["ids"]["student01"], "solved_count": "x"}]},
    ):
        response = api["client"].post(
            f"/assessment-tests/{test_id}/scores", json=payload,
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 400, response.text
        assert response.json()["code"] == 40001

    with SessionLocal(bind=api["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(TestScore)) == 0


def test_scores_require_training_approve(assessment_api):
    api = assessment_api
    test_id = api["create_test"]()
    csrf = api["member_csrf"]("student01")
    response = api["client"].post(
        f"/assessment-tests/{test_id}/scores",
        json={"rows": [_score_row(api["ids"]["student01"])]},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 403
    assert response.json()["code"] == 40301


def test_batch_rolls_back_good_row_before_invalid_row(assessment_api):
    api = assessment_api
    test_id = api["create_test"]()
    csrf = api["member_csrf"]("coach0001")
    response = api["client"].post(
        f"/assessment-tests/{test_id}/scores",
        json={"rows": [
            {**_score_row(api["ids"]["student01"]), "version": 0},
            {**_score_row(api["ids"]["student02"], score=-1), "version": 0},
        ]}, headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400 and response.json()["code"] == 40001
    with SessionLocal(bind=api["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(TestScore)) == 0
        assert session.scalar(select(AuditLog.id).where(AuditLog.action == "score.created")) is None


def test_two_score_forms_preserve_independent_edits_and_report_same_row_conflict(
    assessment_api, client_factory, signed_in,
):
    api = assessment_api
    a, b = (api["ids"][name] for name in ("student01", "student02"))
    test_id = api["create_test"]()
    api["record"](test_id, [_score_row(a, solved=0), _score_row(b)])
    path = f"/assessment-tests/{test_id}/scores"
    first, second = client_factory(), client_factory()
    first_csrf = signed_in(first, "coach0001")
    second_csrf = signed_in(second, "coach0001")

    def form(browser, csrf):
        snapshot = browser.get(path).json()["data"]["items"]
        result = {"csrf_token": csrf, "member_ids": [str(a), str(b)]}
        for item in snapshot:
            member_id = item["id"]
            if member_id in (a, b):
                result[f"version_{member_id}"] = str(item["version"])
                for field, value in (("solved", item["solved_count"]), ("score", item["score"])):
                    result[f"old_{field}_{member_id}"] = result[f"{field}_{member_id}"] = str(value)
        return result

    first_form = form(first, first_csrf)
    second_form = form(second, second_csrf)
    assert first_form[f"solved_{a}"] == "0"
    with SessionLocal(bind=api["engine"]) as session, session.begin():
        session.scalar(select(TestScore).where(TestScore.member_id == a)).kp_snapshot = '{"old": true}'

    first_form[f"score_{a}"] = "90"
    assert first.post(path, data=first_form).json()["data"] == {"created": 0, "updated": 1}
    second_form[f"score_{b}"] = "95"
    assert second.post(path, data=second_form).json()["data"] == {"created": 0, "updated": 1}
    second_form[f"score_{a}"] = "85"
    conflict = second.post(path, data=second_form)
    assert conflict.status_code == 409 and conflict.json()["code"] == 40902
    assert "刷新" in conflict.json()["message"]
    with SessionLocal(bind=api["engine"]) as session:
        scores = {row.member_id: row for row in session.scalars(select(TestScore))}
        assert (scores[a].score, scores[a].solved_count, scores[a].kp_snapshot) == ("90.00", 0, '{"old": true}')
        assert scores[b].score == "95.00"
