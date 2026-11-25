"""UI 层样式迁移（issue #1）的模板渲染与前端约定测试。

只断言语义与结构，不依赖具体旧类名；类名迁移本身由实现负责。
"""

import re


def _assert_confirm_button(html, action, label, message):
    form = re.search(rf'<form\b[^>]*\saction="{re.escape(action)}"[^>]*>(.*?)</form>', html, re.S)
    assert form
    assert "data-confirm" not in form[0].split(">", 1)[0]
    buttons = re.findall(r"<button\b([^>]*)>(.*?)</button>", form[1], re.S)
    matching = [attrs for attrs, content in buttons if re.sub(r"<[^>]+>", "", content).strip() == label]
    assert len(matching) == 1
    assert re.search(r'\stype="submit"(?:\s|$)', matching[0])
    assert re.search(rf'\sdata-confirm="{re.escape(message)}"(?:\s|$)', matching[0])


def _html(client, path, **kwargs):
    response = client.get(path, headers={"Accept": "text/html"}, **kwargs)
    assert response.status_code == 200, (path, response.status_code, response.text)
    assert response.headers["Content-Type"].startswith("text/html")
    return response.text


def test_base_unauthenticated_contains_login_and_apply(client):
    html = _html(client, "/login")
    assert "登录" in html
    assert "入会申请" in html
    target = re.search(r'<button\b[^>]*\bdata-bs-toggle="collapse"[^>]*\bdata-bs-target="#([^"]+)"', html)
    assert target
    assert re.search(rf'<[^>]+\sid="{re.escape(target[1])}"', html)


def test_base_authenticated_contains_member_name_logout_and_toggler(client, accounts, signed_in):
    signed_in(client, "student01")
    html = _html(client, "/members")
    assert "会员 student01" in html
    assert "登出" in html
    assert "navbar-toggler" in html


def test_nav_shows_mine_awards_and_hides_stats_and_export_for_member(client, accounts, signed_in):
    signed_in(client, "student01")
    html = _html(client, "/members")
    assert 'href="/awards?scope=mine"' in html
    assert 'href="/stats/members"' not in html
    assert 'href="/members/export"' not in html
    assert 'href="/problems"' in html
    assert 'href="/training-tasks"' in html
    assert 'href="/assessments"' in html


def test_nav_shows_awards_and_stats_for_coach(client, coach, signed_in):
    signed_in(client, "coach0001")
    html = _html(client, "/members")
    assert 'href="/awards?scope=mine"' in html
    assert 'href="/stats/members"' in html
    assert 'href="/members/export"' not in html


def test_nav_shows_all_entries_for_sys_admin(client, admin, signed_in):
    signed_in(client, "admin0001")
    html = _html(client, "/members")
    for href in (
        "/members",
        "/problems",
        "/training-tasks",
        "/assessments",
        "/stats/members",
        "/awards?scope=mine",
        "/members/export",
    ):
        assert f'href="{href}"' in html, href


def test_member_problem_stats_pages_have_responsive_badge_and_icons(client, coach, signed_in):
    signed_in(client, "coach0001")
    for path in ("/members", "/problems", "/stats/members"):
        html = _html(client, path)
        if path == "/problems":
            assert 'class="empty-state"' in html
            assert "<table" not in html
        else:
            assert "table-responsive" in html, path
        assert "badge" in html, path
        content = html.split("<main", 1)[1].split("</main>", 1)[0]
        assert re.search(r'<i class="bi bi-[^"]+" aria-hidden="true"></i>', content), path


def test_knowledge_point_delete_button_has_data_confirm(client, coach, signed_in):
    csrf = signed_in(client, "coach0001")
    created = client.post(
        "/knowledge-points",
        json={"name": "待删除知识点"},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 200, created.text
    html = _html(client, "/knowledge-points")
    node_id = created.json()["data"]["id"]
    _assert_confirm_button(html, f"/knowledge-points/{node_id}/delete", "删除", "确定删除该叶子知识点？此操作不可撤销")


def test_task_assign_button_has_data_confirm(training_api):
    api = training_api
    plan_id = api["plan"]()
    phase_id = api["phase"](plan_id)
    problem_ids = api["problems"]("下发确认样题")
    task_id = api["task"](phase_id, problem_ids)
    html = _html(api["client"], f"/training-tasks/{task_id}")
    _assert_confirm_button(html, f"/training-tasks/{task_id}/assign", "设置指派并下发", "确定下发该任务？此操作不可撤销")


def test_expel_confirmation_is_bound_to_authorized_submit_button(client, accounts, coach, signed_in):
    path = f'/members/{accounts[0]["student01"]}'
    signed_in(client, "student01")
    assert f'action="{path}/expel"' not in _html(client, path)
    signed_in(client, "coach0001")
    html = _html(client, path)
    _assert_confirm_button(html, f"{path}/expel", "除名", "确定除名该会员？此操作将撤销其所有会话")


def test_static_assets_are_local_and_app_js_wired(client):
    html = _html(client, "/login")
    assert 'href="/static/bootstrap.min.css"' in html
    assert 'href="/static/bootstrap-icons.css"' in html
    assert 'src="/static/bootstrap.bundle.min.js"' in html
    assert 'src="/static/app.js"' in html
    for forbidden in ("cdn.", "http://", "https://"):
        assert forbidden not in html, forbidden
    for name, media_type in (
        ("bootstrap.min.css", "text/css"),
        ("bootstrap-icons.css", "text/css"),
        ("bootstrap-icons.woff2", "font/woff2"),
        ("bootstrap.bundle.min.js", "javascript"),
        ("app.css", "text/css"),
        ("app.js", "javascript"),
    ):
        asset = client.get(f"/static/{name}")
        assert asset.status_code == 200, name
        assert media_type in asset.headers["Content-Type"], name
        assert asset.content, name


def test_pages_do_not_use_bootstrap3_classes(client, coach, signed_in):
    signed_in(client, "coach0001")
    html = _html(client, "/members")
    for legacy in (
        "navbar-default",
        "panel panel-default",
        "btn btn-default",
        "form-group",
        "pull-right",
        "text-right",
    ):
        assert legacy not in html, legacy
