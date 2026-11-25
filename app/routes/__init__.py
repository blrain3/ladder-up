# 汇总各业务域路由，保持原有注册顺序。

from datetime import datetime as datetime

from fastapi import APIRouter

from bin.job import assess_term

# 保留现有调用方使用的包级依赖入口。
from ..markdown import render as render
from ..oj import MAX_CSV_BYTES as MAX_CSV_BYTES
from . import applications, assessment, auth, members, oj, pages, problems, profile, training
from .applications import ensure_member_for_application as ensure_member_for_application

run_assess_term = assess_term

router = APIRouter()
# 按原文件中各段的顺序挂载，拆开原本被其他域隔开的路由。
router.include_router(auth.router)
# 首页与附件下载原本分列在会员页面前后。
router.include_router(pages.home_router)
router.include_router(members.router)
router.include_router(pages.router)
router.include_router(auth.password_router)
router.include_router(applications.router)
router.include_router(problems.router)
router.include_router(training.router)
router.include_router(assessment.router)
router.include_router(members.lifecycle_router)
router.include_router(profile.router)
router.include_router(oj.router)
router.include_router(training.advice_router)
