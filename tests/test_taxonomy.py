from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import KnowledgePoint
from app.models.base import local_time


def login_admin(client, admin):
    response = client.post(
        "/auth/login", json={"student_no": admin["student_no"], "password": admin["password"]}
    )
    assert response.status_code == 303
    return client.get("/me").json()["data"]["csrf_token"]


def create_kp(client, csrf, name, parent_id=None):
    payload = {"name": name}
    if parent_id is not None:
        payload["parent_id"] = parent_id
    response = client.post("/knowledge-points", json=payload, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def move_kp(client, csrf, node_id, parent_id):
    payload = {} if parent_id is None else {"parent_id": parent_id}
    return client.post(
        f"/knowledge-points/{node_id}/move", json=payload, headers={"X-CSRF-Token": csrf}
    )


def test_duplicate_sibling_name_conflict(client, admin):
    csrf = login_admin(client, admin)
    root = create_kp(client, csrf, "图论")
    create_kp(client, csrf, "最短路", root["id"])
    response = client.post(
        "/knowledge-points", json={"name": "最短路", "parent_id": root["id"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 409
    assert response.json()["code"] == 40901


def test_parent_cannot_be_self(client, admin):
    csrf = login_admin(client, admin)
    node = create_kp(client, csrf, "图论")
    response = move_kp(client, csrf, node["id"], node["id"])
    assert response.status_code == 400
    assert response.json()["code"] == 40002


def test_parent_cannot_be_descendant(client, admin):
    csrf = login_admin(client, admin)
    root = create_kp(client, csrf, "图论")
    child = create_kp(client, csrf, "最短路", root["id"])
    grandchild = create_kp(client, csrf, "Dijkstra", child["id"])
    response = move_kp(client, csrf, root["id"], grandchild["id"])
    assert response.status_code == 400
    assert response.json()["code"] == 40002


def test_depth_over_five_rejected(client, admin):
    csrf = login_admin(client, admin)
    parent = None
    for name in ("一", "二", "三", "四", "五"):
        parent = create_kp(client, csrf, name, parent["id"] if parent else None)
    response = client.post(
        "/knowledge-points", json={"name": "六", "parent_id": parent["id"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert response.json()["code"] == 40002


def test_delete_with_children_rejected_with_count(client, admin):
    csrf = login_admin(client, admin)
    root = create_kp(client, csrf, "图论")
    create_kp(client, csrf, "最短路", root["id"])
    response = client.post(
        f"/knowledge-points/{root['id']}/delete", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 400
    assert response.json()["code"] == 40002
    assert response.json()["data"]["child_count"] == 1


def test_move_recomputes_subtree_depth_and_path(client, admin, engine):
    csrf = login_admin(client, admin)
    root_a = create_kp(client, csrf, "图论")
    child_b = create_kp(client, csrf, "最短路", root_a["id"])
    grandchild_c = create_kp(client, csrf, "Dijkstra", child_b["id"])
    root_d = create_kp(client, csrf, "动态规划")

    response = move_kp(client, csrf, child_b["id"], root_d["id"])
    assert response.status_code == 200
    assert response.json()["data"]["affected"] == 2

    with SessionLocal(bind=engine) as session:
        nodes = {
            kp.id: kp for kp in session.scalars(select(KnowledgePoint)).all()
        }
        assert (nodes[root_a["id"]].depth, nodes[root_a["id"]].path) == (1, f"/{root_a['id']}/")
        assert (nodes[child_b["id"]].depth, nodes[child_b["id"]].path) == (
            2, f"/{root_d['id']}/{child_b['id']}/",
        )
        assert (nodes[grandchild_c["id"]].depth, nodes[grandchild_c["id"]].path) == (
            3, f"/{root_d['id']}/{child_b['id']}/{grandchild_c['id']}/",
        )
        assert nodes[child_b["id"]].parent_id == root_d["id"]


def test_root_leaf_delete_succeeds(client, admin, engine):
    csrf = login_admin(client, admin)
    node = create_kp(client, csrf, "图论")
    response = client.post(
        f"/knowledge-points/{node['id']}/delete", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200
    assert response.json()["data"]["deleted"] is True
    with SessionLocal(bind=engine) as session:
        assert session.get(KnowledgePoint, node["id"]) is None


def test_knowledge_points_render_html(client, admin):
    csrf = login_admin(client, admin)
    node = create_kp(client, csrf, "多项式")
    response = client.get("/knowledge-points", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "多项式" in response.text
    assert f'action="/knowledge-points/{node["id"]}/delete"' in response.text
    assert "<td>入门</td>" in response.text


def test_move_rejects_subtree_with_descendant_over_depth_limit(client, admin):
    csrf = login_admin(client, admin)
    root = create_kp(client, csrf, "移动的根")
    child = create_kp(client, csrf, "移动的子节点", root["id"])
    parent = None
    for name in ("第一层", "第二层", "第三层", "第四层"):
        parent = create_kp(client, csrf, name, parent["id"] if parent else None)

    response = move_kp(client, csrf, root["id"], parent["id"])
    assert response.status_code == 400
    assert response.json()["code"] == 40002
    points = {row["id"]: row for row in client.get("/knowledge-points").json()["data"]["items"]}
    assert points[root["id"]]["parent_id"] is None
    assert points[child["id"]]["path"] == child["path"]


def test_concurrent_moves_cannot_create_cycle(client_factory, admin):
    first, second = client_factory(), client_factory()
    first_csrf, second_csrf = login_admin(first, admin), login_admin(second, admin)
    a = create_kp(first, first_csrf, "图论")["id"]
    b = create_kp(first, first_csrf, "数论")["id"]

    # 在 SQLite 获取写锁前汇合，确保两个请求都已持有独立数据库连接。
    engine = first.app.state.engine
    both_started = Barrier(2, timeout=5)
    connections = set()

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE":
            connections.add(conn.connection.driver_connection)
            both_started.wait()

    event.listen(engine, "before_cursor_execute", before_execute)
    try:
        with ThreadPoolExecutor(max_workers=2) as jobs:
            requests = [
                jobs.submit(move_kp, first, first_csrf, a, b),
                jobs.submit(move_kp, second, second_csrf, b, a),
            ]
            results = [request.result() for request in requests]
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)

    assert len(connections) == 2
    assert sorted((response.status_code, response.json()["code"]) for response in results) == [
        (200, 0), (400, 40002),
    ]
    with SessionLocal(bind=engine) as session:
        nodes = session.scalars(select(KnowledgePoint).order_by(KnowledgePoint.depth)).all()
        root, child = nodes
        assert (root.parent_id, root.depth, root.path) == (None, 1, f"/{root.id}/")
        assert (child.parent_id, child.depth, child.path) == (root.id, 2, f"/{root.id}/{child.id}/")


@pytest.mark.parametrize("is_root", [True, False])
def test_database_rejects_duplicate_sibling_name(client, admin, engine, is_root):
    csrf = login_admin(client, admin)
    parent_id = None if is_root else create_kp(client, csrf, "父级")["id"]
    existing = create_kp(client, csrf, "不能重名", parent_id)
    with SessionLocal(bind=engine) as session:
        session.add(KnowledgePoint(
            name=existing["name"], parent_id=parent_id, depth=existing["depth"],
            path="/99999/", difficulty="进阶", created_at=local_time(),
        ))
        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            session.commit()


def test_edit_knowledge_point_handles_unique_constraint(client, admin):
    csrf = login_admin(client, admin)
    first = create_kp(client, csrf, "已有名称")
    second = create_kp(client, csrf, "待修改")
    url = f"/knowledge-points/{second['id']}"
    headers = {"X-CSRF-Token": csrf}
    conflict = client.put(url, json={"name": first["name"]}, headers=headers)
    assert (conflict.status_code, conflict.json()["code"]) == (409, 40901)
    edited = client.put(url, json={"name": "新名称", "difficulty": "提高"}, headers=headers)
    assert edited.status_code == 200
    assert (edited.json()["data"]["name"], edited.json()["data"]["difficulty"]) == ("新名称", "提高")
