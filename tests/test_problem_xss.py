import pytest


@pytest.fixture
def problem_with_xss(client, signed_in, coach):
    csrf = signed_in(client, "coach0001")
    root = client.post(
        "/knowledge-points", json={"name": "安全"}, headers={"X-CSRF-Token": csrf}
    ).json()["data"]
    statement = "<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>"
    created = client.post(
        "/problems",
        json={
            "title": "XSS 题面", "difficulty": "入门", "problem_type": "程序",
            "kp_ids": [root["id"]], "statement": statement,
            "sample_in": "1 2", "sample_out": "3",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 200, created.text
    return csrf, created.json()["data"]["id"]


def test_statement_script_and_img_are_escaped(client, problem_with_xss):
    _csrf, problem_id = problem_with_xss
    page = client.get(f"/problems/{problem_id}", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "<script" not in page.text
    assert "<img src=x onerror=" not in page.text
    assert "&lt;script&gt;" in page.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in page.text


def test_solution_javascript_link_is_removed(client, problem_with_xss):
    csrf, problem_id = problem_with_xss
    submitted = client.post(
        f"/problems/{problem_id}/solutions",
        json={"content_md": "[x](javascript:alert(1))"},
        headers={"X-CSRF-Token": csrf},
    )
    assert submitted.status_code == 200
    assert "javascript:" not in submitted.json()["data"]["content_html"]
    page = client.get(f"/problems/{problem_id}", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "javascript:" not in page.text


def test_solution_code_and_malformed_links_render_safely(client, problem_with_xss):
    csrf, problem_id = problem_with_xss
    submitted = client.post(
        f"/problems/{problem_id}/solutions",
        json={
            "content_md": "~~~c\na <= b && c\n~~~\n\n"
            "[无效 IPv6](https://[::1) [有效 IPv6](https://[::1]/)\n\n"
            "[缺右括号](https://example.test\n\n[缺右方括号(https://example.test)",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert submitted.status_code == 200
    page = client.get(f"/problems/{problem_id}", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "a &lt;= b &amp;&amp; c" in page.text
    assert "&amp;lt;" not in page.text
    assert "<a>无效 IPv6</a>" in page.text
    assert '<a href="https://[::1]/">有效 IPv6</a>' in page.text
    assert "缺右括号" in page.text and "缺右方括号" in page.text
    assert 'href="https://example.test' not in page.text


def test_render_failure_does_not_save_solution(client, problem_with_xss, monkeypatch):
    csrf, problem_id = problem_with_xss

    def fail(_value):
        raise ValueError("渲染失败")

    with monkeypatch.context() as patch:
        patch.setattr("app.routes.render", fail)
        response = client.post(
            f"/problems/{problem_id}/solutions", json={"content_md": "待渲染"},
            headers={"X-CSRF-Token": csrf},
        )
        assert (response.status_code, response.json()["code"]) == (400, 40001)
    assert client.get(f"/problems/{problem_id}/solutions").json()["data"] == []
