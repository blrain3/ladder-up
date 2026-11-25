import os
import re
import socket
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from alembic.config import Config
from playwright.sync_api import Dialog, Page, expect
from sqlalchemy import select

from alembic import command
from app.db import SessionLocal, make_engine
from app.models import (
    MemberApplication,
    Problem,
    TaskProblem,
    TrainingPhase,
    TrainingPlan,
)
from app.models.base import local_time
from app.models.training import TrainingTask
from bin.seed import seed

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "BrowserFixture123!"
APPLICANT_NO = "202400000001"
APPLICANT_NAME = "冒烟测试申请人"
TASK_TITLE = "冒烟测试训练任务"


@pytest.fixture(scope="session")
def browser_type(playwright):
    return playwright.chromium


@pytest.fixture(scope="session")
def browser_type_launch_args():
    return {"headless": True}


@pytest.fixture(scope="session")
def database(tmp_path_factory):
    directory = tmp_path_factory.mktemp("browser-smoke")
    database_url = f"sqlite:///{directory / 'app.db'}"
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "APP_ENV": "test",
        "COOKIE_SECURE": "false",
        "SESSION_TTL_HOURS": "8",
        "UPLOADS_DIR": str(directory / "uploads"),
        "SEED_ADMIN_PASSWORD": PASSWORD,
        "SEED_DEMO_PASSWORD": PASSWORD,
    }
    with pytest.MonkeyPatch.context() as patch:
        for key in ("DATABASE_URL", "APP_ENV", "SEED_ADMIN_PASSWORD", "SEED_DEMO_PASSWORD"):
            patch.setenv(key, env[key])
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        seed(database_url)
    engine = make_engine(database_url)
    try:
        yield engine, env, directory
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def base_url(database):
    _, env, directory = database
    log_path = directory / "uvicorn.log"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener, log_path.open("wb") as log:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        fd = listener.fileno()
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--app-dir", str(ROOT), "--fd", str(fd)],
            cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT, pass_fds=(fd,),
        )

    url = f"http://127.0.0.1:{port}"
    try:
        deadline = monotonic() + 15
        while monotonic() < deadline and process.poll() is None:
            try:
                with urlopen(f"{url}/health", timeout=0.5) as response:
                    if response.status == 200:
                        break
            except (URLError, TimeoutError, ConnectionError):
                pass
            sleep(0.1)
        else:
            pytest.fail(f"Uvicorn did not become healthy:\n{log_path.read_text()}")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@pytest.fixture
def draft_task(database):
    engine, _, _ = database
    now = local_time()
    today = date.fromisoformat(now[:10])
    with SessionLocal(bind=engine) as session, session.begin():
        plan = TrainingPlan(term=f"{today.year}-{today.year + 1}-1", created_at=now)
        session.add(plan)
        session.flush()
        phase = TrainingPhase(
            plan_id=plan.id, name="练习", start_on=str(today - timedelta(days=1)),
            end_on=str(today + timedelta(days=1)),
        )
        problems = [
            Problem(title=title, source="自建", difficulty="入门", problem_type="程序",
                    created_at=now, updated_at=now)
            for title in ("冒烟样题甲", "冒烟样题乙")
        ]
        session.add_all([phase, *problems])
        session.flush()
        task = TrainingTask(phase_id=phase.id, title=TASK_TITLE, assignee_type="指定会员",
                            status="草稿", created_at=now)
        session.add(task)
        session.flush()
        session.add_all(
            TaskProblem(task_id=task.id, problem_id=problem.id, sort_no=index)
            for index, problem in enumerate(problems, start=1)
        )
        return task.id


def submit_login(page: Page, student_no: str, password: str = PASSWORD):
    page.get_by_label("学号").fill(student_no)
    page.get_by_label("密码", exact=True).fill(password)
    page.get_by_role("button", name="登录", exact=True).click()


def submit_application(page: Page, student_no: str):
    page.goto("/apply")
    page.get_by_label("学号", exact=True).fill(student_no)
    page.get_by_label("姓名", exact=True).fill(APPLICANT_NAME)
    page.get_by_label("我已阅读并知悉观察期规则").check()
    page.get_by_role("button", name="提交申请").click()
    expect(page.get_by_role("heading", name="申请已提交")).to_be_visible()


def test_login(page: Page, base_url):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto("/members")
    # 匿名访问受保护页面返回 401 错误页（不是重定向），页面提供返回登录的入口。
    expect(page.get_by_role("alert")).to_contain_text("请登录后重试")
    page.get_by_role("link", name="返回登录").click()
    expect(page).to_have_url(f"{base_url}/login")
    expect(page.get_by_role("heading", name="登录")).to_be_visible()
    submit_login(page, "demo0001", "wrong-password")
    alert = page.get_by_role("alert")
    expect(alert).to_be_visible()
    expect(alert).to_contain_text("学号或密码错误")
    submit_login(page, "demo0001")
    expect(page).to_have_url(f"{base_url}/members")
    expect(page.get_by_role("heading", name="会员列表")).to_be_visible()
    toggler = page.locator(".navbar-toggler")
    menu = page.locator("#main-nav")
    expect(toggler).to_be_visible()
    expect(menu).to_be_hidden()
    toggler.click()
    expect(toggler).to_have_attribute("aria-expanded", "true")
    expect(menu.get_by_role("link", name="会员", exact=True)).to_be_visible()
    expect(menu.locator(".navbar-text")).to_contain_text("示例会员")
    expect(menu.locator(".navbar-text")).to_be_visible()


def test_anonymous_application(page: Page, database):
    # 附件上传在接口层尚未接通（见 issue #7），这里只验表单提交与落库。
    engine, _, _ = database
    submit_application(page, APPLICANT_NO)

    with SessionLocal(bind=engine) as session:
        applications = session.scalars(select(MemberApplication).where(
            MemberApplication.student_no == APPLICANT_NO,
        )).all()
        assert len(applications) == 1
        application = applications[0]
        assert (application.name, application.status) == (APPLICANT_NAME, "待审核")


def test_admin_admission(page: Page, database, base_url):
    engine, _, _ = database
    with SessionLocal(bind=engine) as session:
        student_no = session.scalar(select(MemberApplication.student_no).where(
            MemberApplication.student_no == APPLICANT_NO, MemberApplication.status == "待审核",
        ))
    if student_no is None:
        # Allow this case to run by itself without consuming E2E-2's applicant identity.
        student_no = "202400000002"
        submit_application(page, student_no)

    page.goto("/login")
    submit_login(page, "admin0001")
    expect(page.locator(".navbar-text")).to_contain_text("系统管理员")
    page.goto("/admin/applications")
    row = page.get_by_role("row").filter(has=page.get_by_role("cell", name=student_no, exact=True))
    expect(row.get_by_role("cell", name=APPLICANT_NAME, exact=True)).to_be_visible()
    expect(row.get_by_text("待审核", exact=True)).to_be_visible()
    row.get_by_label(f"{APPLICANT_NAME}的审核意见").fill("资料齐全")
    row.get_by_role("button", name="通过", exact=True).click()
    expect(row.get_by_text("待面试", exact=True)).to_be_visible()
    row.get_by_label(f"{APPLICANT_NAME}的面试意见").fill("面试合格")
    row.get_by_role("button", name="面试通过", exact=True).click()
    expect(row.get_by_text("已面试", exact=True)).to_be_visible()
    row.get_by_role("button", name="录取", exact=True).click()
    admitted = page.get_by_role("heading", name="录取成功")
    expect(admitted).to_be_visible()
    member_id = admitted.get_attribute("data-member-id")
    assert member_id and member_id.isdecimal()

    page.goto("/members")
    page.get_by_role("link", name=APPLICANT_NAME, exact=True).click()
    page.get_by_role("link", name="查看会员证").click()
    expect(page).to_have_url(f"{base_url}/members/{member_id}/card")
    expect(page.get_by_role("heading", name="拾阶会员证")).to_be_visible()
    details = page.locator("#member-card-details")
    expect(details.locator("dt").get_by_text("姓名", exact=True)).to_be_visible()
    expect(details.locator("dt").get_by_text("编号", exact=True)).to_be_visible()
    expect(details.locator("dd").get_by_text(APPLICANT_NAME, exact=True)).to_be_visible()
    expect(details.locator("dd").get_by_text(member_id, exact=True)).to_be_visible()


def test_member_checkin(page: Page, context, draft_task):
    page.goto("/login")
    submit_login(page, "demo0004")
    expect(page.locator(".navbar-text")).to_contain_text("示例教练")
    page.goto(f"/training-tasks/{draft_task}")
    page.get_by_label("指派类型").select_option(label="指定会员")
    page.get_by_label("指定会员（可多选）").select_option(label="demo0001 示例会员")
    dialogs = []

    def accept_confirmation(dialog: Dialog):
        dialogs.append((dialog.type, dialog.message))
        dialog.accept()

    page.on("dialog", accept_confirmation)
    page.get_by_role("button", name="设置指派并下发").click()
    assert dialogs == [("confirm", "确定下发该任务？此操作不可撤销")]
    expect(page.get_by_text("已下发", exact=True)).to_be_visible()

    context.clear_cookies()
    page.goto("/login")
    submit_login(page, "demo0001")
    expect(page.locator(".navbar-text")).to_contain_text("示例会员")
    page.get_by_role("link", name="训练", exact=True).click()
    page.get_by_role("link", name=TASK_TITLE, exact=True).click()
    expect(page.get_by_text("已下发", exact=True)).to_be_visible()
    go_to_checkin = page.get_by_role("link", name="去打卡")
    go_to_checkin.click()
    submit = page.get_by_role("button", name="提交打卡")

    def fill_checkin():
        page.get_by_role("checkbox", name=re.compile("冒烟样题甲")).check()
        expect(page.get_by_role("checkbox", name=re.compile("冒烟样题乙"))).not_to_be_checked()
        page.get_by_label("已解题数").fill("1")
        page.get_by_label("耗时（分钟）").fill("15")

    today = page.locator('input[type="hidden"][name="checkin_date"]').input_value()
    fill_checkin()
    submit.click()
    record = page.get_by_role("table", name="我的打卡记录").get_by_role("row").filter(has_text=today)
    expect(record.get_by_role("cell", name="打卡", exact=True)).to_be_visible()
    expect(record.get_by_role("cell", name="1", exact=True)).to_be_visible()

    go_to_checkin.click()
    fill_checkin()
    submit.click()
    alert = page.get_by_role("alert")
    expect(alert).to_be_visible()
    expect(alert).to_contain_text("今日已打卡，不能重复提交")
