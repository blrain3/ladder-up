import pytest


@pytest.fixture
def library(client, signed_in, coach):
    """建好知识点树与检索对照题，返回各资源 id。"""
    csrf = signed_in(client, "coach0001")

    def create_kp(name, parent_id=None):
        payload = {"name": name}
        if parent_id is not None:
            payload["parent_id"] = parent_id
        response = client.post("/knowledge-points", json=payload, headers={"X-CSRF-Token": csrf})
        assert response.status_code == 200, response.text
        return response.json()["data"]["id"]

    def create_problem(title, kp_id, *, difficulty="入门", problem_type="程序", source="自建"):
        response = client.post(
            "/problems",
            json={
                "title": title, "difficulty": difficulty, "problem_type": problem_type,
                "source": source, "kp_ids": [kp_id],
                "sample_in": "1 2", "sample_out": "3",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200, response.text
        return response.json()["data"]["id"]

    graph = create_kp("图论")
    shortest = create_kp("最短路", graph)
    dp = create_kp("动态规划")
    knapsack = create_kp("背包", dp)

    p1 = create_problem("最短路入门", shortest)
    p2 = create_problem("图论基础", graph, difficulty="提高", problem_type="单选")
    p3 = create_problem("背包问题", knapsack, source="Codeforces")
    p4 = create_problem("状态压缩", knapsack, difficulty="进阶", problem_type="填空", source="Vjudge")

    p5 = create_problem("只匹配难度", graph, problem_type="单选")
    p6 = create_problem("只匹配题型", graph, difficulty="提高")

    return {
        "p5": p5, "p6": p6,
        "csrf": csrf, "graph": graph, "shortest": shortest, "dp": dp, "knapsack": knapsack,
        "p1": p1, "p2": p2, "p3": p3, "p4": p4,
    }


def search(client, **params):
    response = client.get("/problems", params=params)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    return data, {item["id"] for item in data["items"]}


def test_subtree_filter_parent_includes_child_problems(client, library):
    data, ids = search(client, kp=library["graph"])
    assert data["total"] == 4
    assert ids == {library[key] for key in ("p1", "p2", "p5", "p6")}


def test_subtree_filter_child_excludes_parent_problems(client, library):
    data, ids = search(client, kp=library["shortest"])
    assert data["total"] == 1
    assert ids == {library["p1"]}


def test_difficulty_filter(client, library):
    data, ids = search(client, difficulty="入门")
    assert data["total"] == 3
    assert ids == {library["p1"], library["p3"], library["p5"]}


def test_type_filter(client, library):
    data, ids = search(client, type="程序")
    assert data["total"] == 3
    assert ids == {library["p1"], library["p3"], library["p6"]}


def test_source_filter(client, library):
    data, ids = search(client, source="Codeforces")
    assert data["total"] == 1
    assert ids == {library["p3"]}


def test_keyword_filter(client, library):
    data, ids = search(client, keyword="最短路")
    assert data["total"] == 1
    assert ids == {library["p1"]}


def test_combined_filters(client, library):
    data, ids = search(client, kp=library["graph"], difficulty="入门", type="程序")
    assert data["total"] == 1
    assert ids == {library["p1"]}


def test_empty_result_shows_placeholder_and_total_zero(client, library):
    data, _ = search(client, keyword="不存在的关键词")
    assert data["total"] == 0
    assert data["items"] == []
    page = client.get("/problems", params={"keyword": "不存在的关键词"},
                      headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "没有匹配的题目" in page.text


def test_pagination_size_one_second_page(client, library):
    response = client.get("/problems", params={"page": 2, "size": 1})
    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["total"], data["page"], data["size"]) == (6, 2, 1)
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == library["p5"]


def test_empty_filters_and_literal_search(client, library):
    data, _ = search(client, kp="", difficulty="全部", type=" 全部 ", source="", keyword="全部")
    assert data["total"] == 6
    assert search(client, keyword="%_")[0]["total"] == 0
    assert search(client, source=" codeforces ")[0]["total"] == 0
    assert search(client, source=" Codeforces ")[1] == {library["p3"]}


def test_pagination_preserves_all_filters(client, library):
    from html import unescape
    from re import search as match
    from urllib.parse import parse_qs, urlsplit

    response = client.get(
        "/problems",
        params={
            "kpId": library["graph"], "difficulty": "入门", "problemType": "程序",
            "source": "自建", "keyword": "最短路", "page": 2, "size": 1,
        },
        headers={"Accept": "text/html"},
    )
    assert response.status_code == 200
    href = unescape(match(r'<a href="([^"]+)">上一页</a>', response.text)[1])
    assert parse_qs(urlsplit(href).query) == {
        "kp": [str(library["graph"])], "difficulty": ["入门"], "type": ["程序"],
        "source": ["自建"], "keyword": ["最短路"], "page": ["1"], "size": ["1"],
    }
    linked = client.get("/problems" + href).json()["data"]
    assert [item["id"] for item in linked["items"]] == [library["p1"]]
