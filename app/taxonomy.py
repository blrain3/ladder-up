"""知识点树的纯业务规则：命名、父级校验、移动与删除判定."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import KnowledgePoint

MAX_DEPTH = 5


class TaxonomyError(ValueError):
    """业务规则错误，`code` 对应接口错误码 40001/40002."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _fullwidth_to_halfwidth(value: str) -> str:
    result = []
    for char in value:
        code = ord(char)
        if code == 0x3000:
            result.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        else:
            result.append(char)
    return "".join(result)


def normalise_name(value: str) -> str:
    """去空白、全角转半角，并校验长度 1–40。"""
    if not isinstance(value, str):
        raise ValueError("知识点名称须为 1–40 个字符")
    name = _fullwidth_to_halfwidth(value).strip()
    if not 1 <= len(name) <= 40:
        raise ValueError("知识点名称须为 1–40 个字符")
    return name


def validate_parent(session: Session, node_id: int | None, parent_id: int | None) -> None:
    """校验父级：存在性、不得为自身或后代、层级不超过 5。"""
    if parent_id is None:
        return
    parent = session.get(KnowledgePoint, parent_id)
    if parent is None:
        raise TaxonomyError(40001, "父级知识点不存在")
    if node_id is not None:
        node = session.get(KnowledgePoint, node_id)
        if node is None:
            raise TaxonomyError(40001, "知识点不存在")
        if node_id == parent_id:
            raise TaxonomyError(40002, "知识点不能设为自身的父级")
        if parent.path.startswith(node.path):
            raise TaxonomyError(40002, "不能将知识点移动到自身后代之下")
    if (parent.depth or 1) + 1 > MAX_DEPTH:
        raise TaxonomyError(40002, "知识点层级不能超过 5 级")


def next_depth_path(parent: KnowledgePoint | None) -> tuple[int, str]:
    """返回 (新节点深度, 路径前缀)；根为 (1, '/')。"""
    if parent is None:
        return (1, "/")
    return (parent.depth + 1, parent.path)


def descendant_prefix(node: KnowledgePoint) -> str:
    """返回 node.path，用于过滤该节点及全部后代。"""
    return node.path


def move_subtree(session: Session, node_id: int, new_parent_id: int | None) -> int:
    """把 node_id 移动到 new_parent_id 下，重算整棵子树的 depth/path，返回受影响节点数。"""
    validate_parent(session, node_id, new_parent_id)
    node = session.get(KnowledgePoint, node_id)
    if node is None:
        raise TaxonomyError(40001, "知识点不存在")
    if node.parent_id == new_parent_id:
        return 0
    new_parent = session.get(KnowledgePoint, new_parent_id) if new_parent_id is not None else None
    new_depth, new_prefix = next_depth_path(new_parent)
    old_prefix = node.path
    old_depth = node.depth
    rows = session.scalars(
        select(KnowledgePoint)
        .where(KnowledgePoint.path.like(old_prefix + "%"))
        .order_by(KnowledgePoint.depth, KnowledgePoint.id)
    ).all()
    if not rows:
        rows = [node]
    if new_depth + max(row.depth - old_depth for row in rows) > MAX_DEPTH:
        raise TaxonomyError(40002, "移动后的知识点层级不能超过 5 级")
    for row in rows:
        tail = row.path[len(old_prefix):]
        row.path = new_prefix + str(node.id) + "/" + tail
        row.depth = new_depth + (row.depth - old_depth)
    node.parent_id = new_parent_id
    return len(rows)


def can_delete(session: Session, node_id: int) -> int:
    """返回直接子节点数；0 表示叶子节点，可删除。"""
    return session.scalar(
        select(func.count()).select_from(KnowledgePoint).where(KnowledgePoint.parent_id == node_id)
    ) or 0
