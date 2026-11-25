"""视觉规范第 9 章的模板和空列表探针。"""
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alembic.config import Config
from fastapi.testclient import TestClient

from alembic import command

ASSERTIONS = (
    "每页一个 h1（错误页单独判定）",
    "表格有响应式容器",
    "表单页用居中卡片",
    "无内联样式",
    "无 CDN 引用",
    "危险操作带确认",
    "图标对辅助技术隐藏",
    "表单控件有对应标签",
    "app.css ≤ 8192 字节",
    "空列表显示空状态",
)
VOID_TAGS = frozenset("area base br col embed hr img input link meta param source track wbr".split())
JINJA = re.compile(r"{%.*?%}|{#.*?#}|{{.*?}}", re.S)
MACRO = re.compile(r"{%[-+]?\s*macro\b.*?{%[-+]?\s*endmacro\s*[-+]?%}", re.S)
DANGEROUS = re.compile("删除|除名|作废|退会|下发")
INLINE_ACTION = re.compile(r"/(?:logout|delete|expel|void|leave|unbind|publish)/?$")


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    line: int
    parent: "Element | None"
    text: list[str] = field(default_factory=list)
    has_row: bool = False

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def ancestors(self) -> Iterator["Element"]:
        parent = self.parent
        while parent is not None:
            yield parent
            parent = parent.parent


class Markup(HTMLParser):
    def __init__(self, source: str):
        super().__init__()
        self.elements: list[Element] = []
        self.stack: list[Element] = []
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        parent = self.stack[-1] if self.stack else None
        element = Element(tag, {key: value or "" for key, value in attrs}, self.getpos()[0], parent)
        self.elements.append(element)
        if tag not in VOID_TAGS:
            self.stack.append(element)
        if tag == "tr":
            for ancestor in element.ancestors():
                if ancestor.tag == "tbody":
                    ancestor.has_row = True
                    break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        for element in reversed(self.stack):
            if element.tag in {"a", "button"}:
                element.text.append(data)
                break


def template_markup(source: str) -> Markup:
    def replace(match: re.Match[str]) -> str:
        content = match[0]
        if content.startswith("{{"):
            # 保留表达式本身，便于比较动态 id 与 for，保护属性引号。
            return "".join(
                f"&#{ord(char)};" if char in "\"'<> &" else char for char in content
            )
        return "".join("\n" if char == "\n" else " " for char in content)

    return Markup(JINJA.sub(replace, source))


def check_template(path: Path, failures: list[list[str]]) -> None:
    def fail(number: int, line: int, detail: str) -> None:
        failures[number - 1].append(f"{path.name}:{line} {detail}")

    source = path.read_text(encoding="utf-8")
    elements = template_markup(source).elements
    # The base layout and included OJ navigation have no page heading of their own.
    is_page = path.name not in {"base.html", "oj_nav.html"}
    if path.name == "error.html":
        classes = set().union(*(element.classes for element in elements))
        required = {"justify-content-center", "col-lg-5", "card", "bi-exclamation-triangle",
                    "fs-1", "text-danger", "text-muted", "small"}
        links = {element.attrs.get("href") for element in elements if "btn" in element.classes}
        missing = sorted((required - classes) | ({"/login", "/members"} - links))
        if missing:
            fail(1, 1, "错误页缺少 " + ", ".join(missing))
    if is_page:
        content = MACRO.sub(lambda match: "\n" * match[0].count("\n"), source)
        headings = [element for element in template_markup(content).elements if element.tag == "h1"]
        if len(headings) != 1:
            fail(1, headings[1].line if len(headings) > 1 else 1, f"h1 数量为 {len(headings)}")

    labels = {element.attrs.get("for") for element in elements if element.tag == "label"}
    for element in elements:
        tag, attrs, line = element.tag, element.attrs, element.line
        if tag == "table" and not any("table-responsive" in node.classes for node in element.ancestors()):
            fail(2, line, "table 缺少 .table-responsive 容器")
        if tag == "form":
            ancestors = list(element.ancestors())
            inline = INLINE_ACTION.search(attrs.get("action", "")) or any(
                node.tag in {"td", "th"} for node in ancestors
            )
            surrounding_classes = element.classes.union(*(node.classes for node in ancestors))
            if not inline and not {"justify-content-center", "card"} <= surrounding_classes:
                fail(3, line, "form 缺少居中卡片")
        if tag == "style" or "style" in attrs:
            fail(4, line, f"<{tag}> 含内联样式")
        for attr in ("src", "href"):
            if re.search(r"https?://", attrs.get(attr, ""), re.I):
                fail(5, line, f"<{tag}> 的 {attr} 含外部 URL")
        if (tag == "button" or (tag == "a" and "btn" in element.classes)) and not attrs.get("data-confirm"):
            if DANGEROUS.search("".join(element.text).strip()):
                fail(6, line, f"<{tag}> 缺少 data-confirm")
        if "bi" in element.classes and any(value.startswith("bi-") for value in element.classes):
            if attrs.get("aria-hidden") != "true":
                fail(7, line, f"<{tag}> 缺少 aria-hidden=\"true\"")
        if tag in {"input", "select", "textarea"} and not (
            attrs.get("type", "").lower() in {"hidden", "submit"} or attrs.get("name") == "csrf_token"
        ):
            if not attrs.get("id") or attrs["id"] not in labels:
                fail(8, line, f"<{tag}> {attrs.get('id') or attrs.get('name', '')} 缺少对应 label[for]")


def check_empty_lists(directory: Path, failures: list[str]) -> None:
    database_url = f"sqlite:///{directory / 'style.db'}"
    os.environ.update(DATABASE_URL=database_url, APP_ENV="probe",
                      SESSION_TTL_HOURS="8", UPLOADS_DIR=str(directory / "uploads"))
    from app.db import SessionLocal, make_engine
    from app.main import app
    from app.models import Member
    from app.models.base import local_time
    from app.security import hash_password

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")
    student_no = "style_admin"
    password = "StyleProbe1!"
    now = local_time()
    engine = make_engine(database_url)
    try:
        with SessionLocal(bind=engine) as session, session.begin():
            session.add(Member(
                student_no=student_no, name="样式探针", role="sys_admin",
                member_type="管理会员", member_status="正式会员",
                password_hash=hash_password(password), created_at=now, updated_at=now,
            ))
    finally:
        engine.dispose()

    with TestClient(app, base_url="https://testserver", headers={"Accept": "text/html"},
                    follow_redirects=False) as client:
        login = client.post("/auth/login", data={"student_no": student_no, "password": password})
        if login.status_code != 303:
            failures.append(f"login.html form: HTTP {login.status_code}")
            return
        # 唯一账号用于认证；请求第二页验证会员列表的空结果。
        for template, url in (
            ("members.html", "/members?page=2"), ("problems.html", "/problems"),
            ("awards.html", "/awards"), ("training_tasks.html", "/training-tasks"),
        ):
            response = client.get(url)
            if response.status_code != 200:
                failures.append(f"{template} ({url}): HTTP {response.status_code}")
                continue
            elements = Markup(response.text).elements
            missing = []
            if any(element.tag == "tbody" and not element.has_row for element in elements):
                missing.append("出现空 tbody")
            elif any(element.tag == "table" for element in elements):
                missing.append("空结果仍显示表格")
            if not any("empty-state" in element.classes for element in elements):
                missing.append("缺少 .empty-state")
            if missing:
                failures.append(f"{template}: {', '.join(missing)}")


def main() -> int:
    failures: list[list[str]] = [[] for _ in ASSERTIONS]
    for path in sorted((ROOT / "app/templates").glob("*.html")):
        check_template(path, failures)

    css_size = (ROOT / "app/static/app.css").stat().st_size
    if css_size > 8192:
        failures[8].append(f"app.css: {css_size} 字节")

    with TemporaryDirectory(prefix="probe-style-") as directory:
        original_directory = Path.cwd()
        try:
            os.chdir(directory)
            check_empty_lists(Path(directory), failures[9])
        except Exception as exc:
            failures[9].append(f"临时库或列表请求失败: {type(exc).__name__}: {exc}")
        finally:
            os.chdir(original_directory)

    for number, (name, issues) in enumerate(zip(ASSERTIONS, failures, strict=True), start=1):
        supplemental = "；".join(issues) if issues else f"{css_size} 字节" if number == 9 else ""
        verdict = "FAIL" if issues else "PASS"
        print(f"{verdict}\tST-{number:02d} {name}\t{supplemental}")

    failed = sum(bool(issues) for issues in failures)
    print(f"\n合计 {len(ASSERTIONS)} 项，失败 {failed} 项")
    return int(failed > 0)


if __name__ == "__main__":
    sys.exit(main())
