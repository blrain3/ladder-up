# 知识点树、题库、检索和题解。
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import ColumnElement, delete, false, func, inspect, or_, select, text
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from .. import routes
from ..auth import (
    audit,
    require_permission,
)
from ..db import get_session
from ..models import (
    KnowledgePoint,
    Member,
    Problem,
    ProblemKp,
    ProblemSolution,
)
from ..models.base import local_time
from ..taxonomy import (
    TaxonomyError,
    can_delete,
    move_subtree,
    next_depth_path,
    normalise_name,
    validate_parent,
)
from ..web import page, parse_object_id, success, wants_html
from ._common import (
    api_error,
    bank_write,
    enum_field,
    int_field,
    object_id,
    optional_text,
    payload,
    redirect,
    required_text,
)

router = APIRouter()


PROBLEM_TYPES = ("单选", "多选", "程序", "填空")
PROBLEM_DIFFICULTIES = ("入门", "提高", "进阶", "区域赛金", "区域赛银", "区域赛铜")
KP_DIFFICULTIES = ("入门", "提高", "进阶", "区域赛")
PROBLEM_READER = require_permission("problem:read", target_type="problem")
PROBLEM_WRITER = require_permission("problem:write", target_type="problem")


def markdown_text(data, name, limit):
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise api_error(400, 40001, "参数格式不正确")
    if len(value) > limit:
        raise api_error(400, 40001, f"{name}不能超过 {limit} 个字符")
    return value or None


def kp_ids_field(data):
    if isinstance(data, FormData):
        raw = data.getlist("kp_ids") or data.get("kp_id")
    else:
        raw = data.get("kp_ids", data.get("kp_id"))
    if raw in (None, ""):
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise api_error(400, 40001, "知识点参数无效")
    parts = [
        part.strip()
        for item in raw
        for part in str(item).replace("，", ",").split(",")
        if part.strip()
    ]
    ids = []
    for part in parts:
        try:
            ids.append(parse_object_id(part))
        except ValueError:
            raise api_error(400, 40001, "知识点参数无效") from None
    return ids


def knowledge_point_count_map(session):
    rows = session.execute(
        select(ProblemKp.kp_id, func.count())
        .select_from(ProblemKp)
        .join(Problem, Problem.id == ProblemKp.problem_id)
        .where(Problem.deleted_at.is_(None))
        .group_by(ProblemKp.kp_id)
    ).all()
    return dict(rows)


def knowledge_point_item(kp, count_map):
    return {
        "id": kp.id, "name": kp.name, "parent_id": kp.parent_id,
        "depth": kp.depth, "path": kp.path, "difficulty": kp.difficulty,
        "problem_count": count_map.get(kp.id, 0),
    }


def problem_kps(session, problem_ids):
    grouped: dict[int, list[dict[str, int | str]]] = {problem_id: [] for problem_id in problem_ids}
    if grouped:
        rows = session.execute(
            select(ProblemKp.problem_id, KnowledgePoint)
            .join(KnowledgePoint, KnowledgePoint.id == ProblemKp.kp_id)
            .where(ProblemKp.problem_id.in_(grouped))
            .order_by(KnowledgePoint.path)
        )
        for problem_id, kp in rows:
            grouped[problem_id].append({"id": kp.id, "name": kp.name, "path": kp.path})
    return grouped


def problem_solutions_payload(solutions, session):
    payload = []
    for solution in solutions:
        author = session.get(Member, solution.author_id) if solution.author_id else None
        payload.append({
            "id": solution.id, "author_id": solution.author_id,
            "author_name": author.name if author else None,
            "created_at": solution.created_at, "content_md": solution.content_md,
            "content_html": routes.render(solution.content_md),
        })
    return payload


def problem_solutions(problem_id, session):
    solutions = session.scalars(
        select(ProblemSolution).where(ProblemSolution.problem_id == problem_id)
        .order_by(ProblemSolution.id)
    ).all()
    return problem_solutions_payload(solutions, session)


def problem_payload(problem, session):
    return {
        "id": problem.id, "title": problem.title, "source": problem.source,
        "platform": problem.platform, "external_id": problem.external_id,
        "difficulty": problem.difficulty, "problem_type": problem.problem_type,
        "statement": problem.statement, "statement_html": routes.render(problem.statement or ""),
        "sample_in": problem.sample_in, "sample_in_html": routes.render(problem.sample_in or ""),
        "sample_out": problem.sample_out, "sample_out_html": routes.render(problem.sample_out or ""),
        "created_at": problem.created_at, "updated_at": problem.updated_at,
        "kps": problem_kps(session, [problem.id])[problem.id],
        "solutions": problem_solutions(problem.id, session),
    }


def problem_item(problem, kps, solution_count):
    return {
        "id": problem.id, "title": problem.title, "source": problem.source,
        "difficulty": problem.difficulty, "problem_type": problem.problem_type,
        "platform": problem.platform, "external_id": problem.external_id,
        "created_at": problem.created_at,
        "kp_names": [kp["name"] for kp in kps],
        "solution_count": solution_count,
    }


def validated_render(value):
    try:
        return routes.render(value or "")
    except (ValueError, RecursionError):
        raise api_error(400, 40001, "Markdown 内容无法渲染") from None


def apply_problem(data, session, problem, *, member):
    title = required_text(data, "title", "标题", 200)
    difficulty = enum_field(data, "difficulty", PROBLEM_DIFFICULTIES, "难度")
    problem_type = enum_field(data, "problem_type", PROBLEM_TYPES, "题型")
    source = optional_text(data, "source", 100)
    platform = optional_text(data, "platform", 40)
    external_id = optional_text(data, "external_id", 100)
    statement = markdown_text(data, "statement", 20000)
    samples = {
        name: markdown_text(data, name, 20000) if problem is None or name in data
        else getattr(problem, name)
        for name in ("sample_in", "sample_out")
    }
    sample_in, sample_out = samples.values()
    if problem is None or samples.keys() & data.keys():
        if not all(sample and sample.strip() for sample in samples.values()):
            raise api_error(400, 40001, "请填写样例输入和样例输出")
    for value in (statement, sample_in, sample_out):
        validated_render(value)

    if platform and external_id:
        statement_row = select(Problem).where(
            Problem.platform == platform, Problem.external_id == external_id,
        )
        if problem is not None:
            statement_row = statement_row.where(Problem.id != problem.id)
        existing = session.scalar(statement_row)
        if existing is not None:
            raise api_error(409, 40901, "该平台的外部题目已存在", {"id": existing.id})

    if problem is None:
        problem = Problem(
            title=title, source=source, platform=platform, external_id=external_id,
            difficulty=difficulty, problem_type=problem_type, statement=statement,
            sample_in=sample_in, sample_out=sample_out,
            created_by=member.id, created_at=local_time(), updated_at=local_time(),
        )
        session.add(problem)
        session.flush()
    else:
        problem.title = title
        problem.source = source
        problem.platform = platform
        problem.external_id = external_id
        problem.difficulty = difficulty
        problem.problem_type = problem_type
        problem.statement = statement
        problem.sample_in = sample_in
        problem.sample_out = sample_out
        problem.updated_at = local_time()
    return problem


def link_problem_kps(session, problem_id, kp_ids):
    for kp_id in dict.fromkeys(kp_ids):
        if session.get(KnowledgePoint, kp_id) is None:
            raise api_error(400, 40002, "关联的知识点不存在")
    session.execute(delete(ProblemKp).where(ProblemKp.problem_id == problem_id))
    for kp_id in dict.fromkeys(kp_ids):
        session.add(ProblemKp(problem_id=problem_id, kp_id=kp_id))


@router.get("/knowledge-points")
def knowledge_points(request: Request, member: Member = Depends(PROBLEM_READER),
                     session: Session = Depends(get_session)):
    points = session.scalars(
        select(KnowledgePoint).order_by(KnowledgePoint.path, KnowledgePoint.id)
    ).all()
    count_map = knowledge_point_count_map(session)
    data = {
        "total": len(points),
        "items": [knowledge_point_item(kp, count_map) for kp in points],
    }
    if wants_html(request):
        return page(request, "knowledge_points.html", tree=data)
    return success(data)


@router.post("/knowledge-points")
def create_knowledge_point(request: Request, data=Depends(payload),
                           member: Member = Depends(PROBLEM_WRITER),
                           session: Session = Depends(get_session)):
    with bank_write(session, "同级知识点已存在同名项"):
        raw_name = data.get("name")
        if not isinstance(raw_name, str):
            raise api_error(400, 40001, "请填写知识点名称")
        try:
            name = normalise_name(raw_name)
        except ValueError as exc:
            raise api_error(400, 40001, str(exc)) from None

        parent_id = int_field(data, "parent_id")
        try:
            validate_parent(session, None, parent_id)
        except TaxonomyError as exc:
            raise api_error(400, exc.code, str(exc)) from None

        duplicate = session.scalar(select(KnowledgePoint.id).where(
            KnowledgePoint.parent_id == parent_id, KnowledgePoint.name == name,
        ))
        if duplicate is not None:
            raise api_error(409, 40901, "同级知识点已存在同名项", {"id": duplicate})

        difficulty = enum_field(data, "difficulty", KP_DIFFICULTIES, "难度", default="入门")
        parent = session.get(KnowledgePoint, parent_id) if parent_id is not None else None
        depth, prefix = next_depth_path(parent)
        node = KnowledgePoint(
            parent_id=parent_id, name=name, difficulty=difficulty, depth=depth, path=prefix,
            created_at=local_time(),
        )
        session.add(node)
        session.flush()
        node.path = prefix + str(node.id) + "/"
        audit(request, session, "knowledge_point.created", actor_id=member.id, target_id=node.id,
              target_type="knowledge_point", detail={"name": name, "parent_id": parent_id})
    if wants_html(request):
        return redirect("/knowledge-points")
    return success({"id": node.id, "name": node.name, "parent_id": node.parent_id,
                    "depth": node.depth, "path": node.path})


@router.put("/knowledge-points/{id}")
def edit_knowledge_point(request: Request, id: int = Depends(object_id), data=Depends(payload),
                         member: Member = Depends(PROBLEM_WRITER),
                         session: Session = Depends(get_session)):
    with bank_write(session, "同级知识点已存在同名项"):
        node = session.get(KnowledgePoint, id)
        if node is None:
            raise api_error(404, 40401, "知识点不存在")
        if "name" in data:
            try:
                node.name = normalise_name(data["name"])
            except ValueError as exc:
                raise api_error(400, 40001, str(exc)) from None
        if "difficulty" in data:
            node.difficulty = enum_field(data, "difficulty", KP_DIFFICULTIES, "难度")
        audit(request, session, "knowledge_point.updated", actor_id=member.id, target_id=id,
              target_type="knowledge_point", detail={"name": node.name, "difficulty": node.difficulty})
    return success(knowledge_point_item(node, knowledge_point_count_map(session)))


@router.post("/knowledge-points/{id}/move")
def move_knowledge_point(request: Request, id: int = Depends(object_id), data=Depends(payload),
                         member: Member = Depends(PROBLEM_WRITER),
                         session: Session = Depends(get_session)):
    with bank_write(session, "目标位置已存在同名知识点"):
        node = session.get(KnowledgePoint, id)
        if node is None:
            raise api_error(404, 40401, "知识点不存在")
        new_parent_id = int_field(data, "parent_id")
        duplicate = session.scalar(select(KnowledgePoint.id).where(
            KnowledgePoint.parent_id == new_parent_id, KnowledgePoint.name == node.name,
            KnowledgePoint.id != id,
        ))
        if duplicate is not None:
            raise api_error(409, 40901, "目标位置已存在同名知识点", {"id": duplicate})
        try:
            affected = move_subtree(session, id, new_parent_id)
        except TaxonomyError as exc:
            raise api_error(400, exc.code, str(exc)) from None
        audit(request, session, "knowledge_point.moved", actor_id=member.id, target_id=id,
              target_type="knowledge_point", detail={"new_parent_id": new_parent_id, "affected": affected})
    if wants_html(request):
        return redirect("/knowledge-points")
    return success({"id": id, "affected": affected, "depth": node.depth, "path": node.path})


@router.delete("/knowledge-points/{id}")
@router.post("/knowledge-points/{id}/delete")
def delete_knowledge_point(request: Request, id: int = Depends(object_id),
                           member: Member = Depends(PROBLEM_WRITER),
                           session: Session = Depends(get_session)):
    with bank_write(session, "知识点删除冲突"):
        node = session.get(KnowledgePoint, id)
        if node is None:
            raise api_error(404, 40401, "知识点不存在")
        child_count = can_delete(session, id)
        if child_count:
            raise api_error(400, 40002, "该知识点下还有子知识点，不能删除",
                            {"child_count": child_count})
        linked_problems = session.scalar(
            select(func.count()).select_from(ProblemKp).where(ProblemKp.kp_id == id)
        )
        if linked_problems:
            raise api_error(400, 40002, "该知识点下有关联题目，不能删除",
                            {"problem_count": linked_problems})
        session.delete(node)
        audit(request, session, "knowledge_point.deleted", actor_id=member.id, target_id=id,
              target_type="knowledge_point", detail={"name": node.name})
    if wants_html(request):
        return redirect("/knowledge-points")
    return success({"deleted": True, "id": id})


@router.get("/problems/stats")
def problem_stats(request: Request, member: Member = Depends(PROBLEM_READER),
                  session: Session = Depends(get_session)):
    kp_rows = session.execute(
        select(KnowledgePoint.id, KnowledgePoint.name, KnowledgePoint.path, func.count(Problem.id))
        .outerjoin(ProblemKp, ProblemKp.kp_id == KnowledgePoint.id)
        .outerjoin(Problem, (Problem.id == ProblemKp.problem_id) & Problem.deleted_at.is_(None))
        .group_by(KnowledgePoint.id, KnowledgePoint.name, KnowledgePoint.path)
        .order_by(KnowledgePoint.path)
    ).all()
    difficulty_rows = session.execute(
        select(Problem.difficulty, func.count())
        .where(Problem.deleted_at.is_(None))
        .group_by(Problem.difficulty)
    ).all()
    type_rows = session.execute(
        select(Problem.problem_type, func.count())
        .where(Problem.deleted_at.is_(None))
        .group_by(Problem.problem_type)
    ).all()
    tables = inspect(session.connection())
    task_refs: dict[int, int] = {}
    if tables.has_table("task_problem"):
        task_refs = dict(session.execute(
            text("SELECT problem_id, count(*) FROM task_problem GROUP BY problem_id")
        ).all())
    correct_rates: dict[int, float] = {}
    if tables.has_table("oj_submission"):
        correct_rates = dict(session.execute(text(
            "SELECT problem_id, avg(CASE WHEN verdict = 'AC' THEN 1.0 ELSE 0.0 END) "
            "FROM oj_submission WHERE problem_id IS NOT NULL GROUP BY problem_id"
        )).all())
    problems = session.scalars(
        select(Problem).where(Problem.deleted_at.is_(None)).order_by(Problem.id)
    ).all()
    data = {
        "kp_counts": [
            {"kp_id": kp_id, "kp_name": name, "kp_path": path, "problem_count": count}
            for kp_id, name, path, count in kp_rows
        ],
        "difficulty_counts": dict(difficulty_rows),
        "type_counts": dict(type_rows),
        "problem_references": [
            {"problem_id": problem.id, "title": problem.title,
             "referenced_count": task_refs.get(problem.id, 0),
             "correct_rate": correct_rates.get(problem.id)}
            for problem in problems
        ],
    }
    if wants_html(request):
        return page(request, "problem_stats.html", stats=data)
    return success(data)


def search_value(value):
    value = (value or "").strip()
    return None if value in ("", "全部") else value


@router.get("/problems")
def problem_list(request: Request, kp: str | None = Query(None),
                 difficulty: str | None = Query(None), type: str | None = Query(None),
                 source: str | None = Query(None), keyword: str | None = Query(None),
                 page_number: int = Query(1, alias="page", ge=1, le=2**63 // 100),
                 size: int = Query(20, ge=1, le=100),
                 member: Member = Depends(PROBLEM_READER),
                 session: Session = Depends(get_session)):
    """筛选值去首尾空白。来源区分大小写且精确匹配，关键词按字面查找，忽略 ASCII 大小写。"""
    kp = search_value(kp if kp is not None else request.query_params.get("kpId"))
    type = search_value(type if type is not None else request.query_params.get("problemType"))
    difficulty, source, keyword = map(search_value, (difficulty, source, keyword))
    filters: list[ColumnElement[bool]] = [Problem.deleted_at.is_(None)]
    if kp is not None:
        kp = int_field({"kp": kp}, "kp")
        node = session.get(KnowledgePoint, kp)
        if node is None:
            filters.append(false())
        else:
            filters.append(Problem.id.in_(
                select(ProblemKp.problem_id).where(ProblemKp.kp_id.in_(
                    select(KnowledgePoint.id).where(KnowledgePoint.path.like(node.path + "%"))
                ))
            ))
    if difficulty:
        filters.append(Problem.difficulty == difficulty)
    if type:
        filters.append(Problem.problem_type == type)
    if source:
        filters.append(Problem.source == source)
    if keyword:
        filters.append(or_(
            Problem.title.contains(keyword, autoescape=True),
            Problem.statement.contains(keyword, autoescape=True),
        ))

    total = session.scalar(select(func.count()).select_from(Problem).where(*filters))
    rows = session.scalars(
        select(Problem).where(*filters).order_by(Problem.id.desc())
        .offset((page_number - 1) * size).limit(size)
    ).all()
    kps = problem_kps(session, [problem.id for problem in rows])
    solution_counts = dict(session.execute(
        select(ProblemSolution.problem_id, func.count())
        .where(ProblemSolution.problem_id.in_(kps))
        .group_by(ProblemSolution.problem_id)
    ).all()) if kps else {}
    data = {
        "total": total, "page": page_number, "size": size,
        "items": [problem_item(problem, kps[problem.id], solution_counts.get(problem.id, 0))
                  for problem in rows],
    }
    if wants_html(request):
        values = {"kp": kp, "difficulty": difficulty, "type": type,
                  "source": source, "keyword": keyword}
        page_query = urlencode({"size": size, **{k: v for k, v in values.items() if v is not None}})
        return page(
            request, "problems.html", pagination=data, filters=values, page_query=page_query,
            knowledge_points=session.scalars(
                select(KnowledgePoint).order_by(KnowledgePoint.path)
            ).all(),
            difficulties=PROBLEM_DIFFICULTIES, types=PROBLEM_TYPES,
        )
    return success(data)


def active_problem(session, problem_id):
    problem = session.get(Problem, problem_id)
    if problem is None or problem.deleted_at is not None:
        raise api_error(404, 40401, "题目不存在")
    return problem


@router.get("/problems/{id}")
def problem_detail(request: Request, id: int = Depends(object_id),
                   member: Member = Depends(PROBLEM_READER),
                   session: Session = Depends(get_session)):
    problem = active_problem(session, id)
    data = problem_payload(problem, session)
    if wants_html(request):
        return page(request, "problem.html", problem=data)
    return success(data)


@router.post("/problems")
def create_problem(request: Request, data=Depends(payload),
                   member: Member = Depends(PROBLEM_WRITER),
                   session: Session = Depends(get_session)):
    with bank_write(session, "该平台的外部题目已存在"):
        kp_ids = kp_ids_field(data)
        if not kp_ids:
            raise api_error(400, 40001, "请选择至少一个知识点")
        problem = apply_problem(data, session, None, member=member)
        link_problem_kps(session, problem.id, kp_ids)
        audit(request, session, "problem.created", actor_id=member.id, target_id=problem.id,
              target_type="problem", detail={"title": problem.title})
    if wants_html(request):
        return redirect(f"/problems/{problem.id}")
    return success({"id": problem.id, "title": problem.title})


@router.put("/problems/{id}")
@router.post("/problems/{id}")
def edit_problem(request: Request, id: int = Depends(object_id), data=Depends(payload),
                 member: Member = Depends(PROBLEM_WRITER),
                 session: Session = Depends(get_session)):
    with bank_write(session, "该平台的外部题目已存在"):
        problem = active_problem(session, id)
        problem = apply_problem(data, session, problem, member=member)
        kp_ids = kp_ids_field(data)
        if kp_ids is not None:
            if not kp_ids:
                raise api_error(400, 40001, "请选择至少一个知识点")
            link_problem_kps(session, problem.id, kp_ids)
        audit(request, session, "problem.updated", actor_id=member.id, target_id=problem.id,
              target_type="problem", detail={"title": problem.title})
    if wants_html(request):
        return redirect(f"/problems/{id}")
    return success({"id": problem.id, "title": problem.title})


@router.delete("/problems/{id}")
def delete_problem(request: Request, id: int = Depends(object_id),
                   member: Member = Depends(PROBLEM_WRITER),
                   session: Session = Depends(get_session)):
    with bank_write(session, "题目删除冲突"):
        problem = active_problem(session, id)
        problem.deleted_at = problem.updated_at = local_time()
        audit(request, session, "problem.deleted", actor_id=member.id, target_id=id,
              target_type="problem", detail={"title": problem.title})
    return success({"id": id, "deleted": True})


@router.get("/problems/{id}/solutions")
def list_solutions(id: int = Depends(object_id), member: Member = Depends(PROBLEM_READER),
                   session: Session = Depends(get_session)):
    active_problem(session, id)
    return success(problem_solutions(id, session))


@router.post("/problems/{id}/solutions")
def create_solution(request: Request, id: int = Depends(object_id), data=Depends(payload),
                    member: Member = Depends(PROBLEM_WRITER),
                    session: Session = Depends(get_session)):
    with bank_write(session, "题解保存冲突"):
        active_problem(session, id)
        content_md = markdown_text(data, "content_md", 20000)
        if not content_md:
            raise api_error(400, 40001, "请填写题解内容")
        content_html = validated_render(content_md)
        solution = ProblemSolution(
            problem_id=id, author_id=member.id, content_md=content_md, created_at=local_time(),
        )
        session.add(solution)
        session.flush()
        audit(request, session, "problem.solution.created", actor_id=member.id,
              target_id=solution.id, target_type="problem_solution", detail={"problem_id": id})
    if wants_html(request):
        return redirect(f"/problems/{id}")
    return success({"id": solution.id, "content_html": content_html})
