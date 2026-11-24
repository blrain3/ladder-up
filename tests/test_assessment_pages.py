import re

from sqlalchemy import select

from app.db import SessionLocal
from app.models import MemberAssessment


def assert_html(response):
    assert response.status_code == 200, response.text
    assert response.headers["Content-Type"].startswith("text/html")
    return response.text


def test_member_left_page_renders_html(assessment_api):
    api = assessment_api
    member_id = api["ids"]["student02"]
    csrf = api["member_csrf"]("student02")
    submitted = api["client"].post(f"/members/{member_id}/leave", json={}, headers={"X-CSRF-Token": csrf})
    assert submitted.json()["data"]["status"] == "待审核"
    csrf = api["member_csrf"]("admin0001")
    html = assert_html(api["client"].post(
        f"/members/{member_id}/leave/approve", data={"csrf_token": csrf}, headers={"Accept": "text/html"},
    ))
    assert "已通过" in html and "该会员已退会，其会话已全部撤销" in html


def test_scores_page_lists_member_inputs(assessment_api):
    api = assessment_api
    test_id = api["create_test"]()
    member_id = api["ids"]["student01"]
    api["record"](test_id, [{"member_id": member_id, "solved_count": 0, "score": 75}])
    html = assert_html(api["client"].get(f"/assessment-tests/{test_id}/scores", headers={"Accept": "text/html"}))
    assert "第一周周测" in html and "student01" in html
    assert f'name="score_{member_id}"' in html and 'value="75.00"' in html
    assert re.search(rf'name="solved_{member_id}"[^>]*value="0"', html)


def test_assessments_page_contains_result_row(assessment_api):
    api = assessment_api
    member_id = api["ids"]["student01"]
    api["record"](api["create_test"](), [{"member_id": member_id, "score": 75}])
    api["run_assess"]()
    html = assert_html(api["client"].get("/assessments?term=2026-2027-1", headers={"Accept": "text/html"}))
    assert re.search(
        r"<td>2026-2027-1</td>\s*<td>student01</td>\s*<td>[^<]+</td>\s*"
        r"<td>\s*<span[^>]*><i[^>]*></i>通过</span>\s*</td>",
        html, re.S,
    )
    assert "最近任务运行" in html


def test_promotions_page_disables_ineligible_member_button(assessment_api):
    api = assessment_api
    member_id = api["ids"]["student01"]
    html = assert_html(api["client"].get("/promotions?term=2026-2027-1", headers={"Accept": "text/html"}))
    assert "student01" in html and "训练完成率不足" in html
    form = re.search(rf'<form[^>]+action="/members/{member_id}/promote">(.*?)</form>', html, re.S)
    assert form is not None
    assert re.search(r"<button\b[^>]*\bdisabled\b[^>]*>", form[1])


def test_assessment_tests_page_contains_created_test(assessment_api):
    api = assessment_api
    test_id = api["create_test"]()
    html = assert_html(api["client"].get("/assessment-tests", headers={"Accept": "text/html"}))
    assert "第一周周测" in html and f'href="/assessment-tests/{test_id}/scores"' in html


def test_mine_assessment_and_promotion_reasons_are_private(assessment_api):
    api = assessment_api
    api["run_assess"]()
    api["member_csrf"]("student01")
    client = api["client"]
    me, other = (api["ids"][name] for name in ("student01", "student02"))
    with SessionLocal(bind=api["engine"]) as session:
        assert session.scalar(select(MemberAssessment.id).where(MemberAssessment.member_id == other))
    for endpoint, id_field in (("/assessments", "member_id"), ("/promotions", "id")):
        private = client.get(endpoint)
        assert private.status_code == 403 and private.json()["code"] == 40301
        result = client.get(f"{endpoint}?scope=mine")
        assert result.status_code == 200
        rows = result.json()["data"]["items"]
        assert [row[id_field] for row in rows] == [me]
        if endpoint == "/promotions":
            assert any("训练完成率不足" in reason for reason in rows[0]["reasons"])
        forbidden = client.get(f"{endpoint}?scope=mine&member_id={other}")
        assert forbidden.status_code == 403
    html = assert_html(client.get("/assessments?scope=mine", headers={"Accept": "text/html"}))
    assert "最近任务运行" not in html and "student02" not in html


def test_http_job_failure_exposes_only_reference(assessment_api, monkeypatch):
    from app import routes

    api = assessment_api

    def fail(*_args):
        raise RuntimeError("secret score and name")

    monkeypatch.setattr(routes, "run_assess_term", fail)
    csrf = api["member_csrf"]("admin0001")
    response = api["client"].post(
        "/assessments/run", json={"term": "2026-2027-1"}, headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 500 and response.json()["code"] == 50001
    assert response.json()["data"]["request_id"] == response.headers["X-Request-ID"]
    assert "secret score and name" not in response.text
    html_error = api["client"].post(
        "/assessments/run", data={"term": "2026-2027-1", "csrf_token": csrf},
        headers={"Accept": "text/html"},
    )
    assert html_error.status_code == 500
    assert f'50001 · request_id {html_error.headers["X-Request-ID"]}' in html_error.text
    assert "secret score and name" not in html_error.text
    api["member_csrf"]("deptadmin")
    html = assert_html(api["client"].get("/assessments", headers={"Accept": "text/html"}))
    assert "最近任务运行" not in html
