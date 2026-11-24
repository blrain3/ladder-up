from pathlib import Path

from fastapi.templating import Jinja2Templates

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=APP_DIR / "templates")


def parse_object_id(value):
    if not isinstance(value, str) or len(value) > 19 or not value.isascii() or not value.isdecimal():
        raise ValueError("Invalid ID")
    number = int(value)
    if not 1 <= number <= 2**63 - 1:
        raise ValueError("Invalid ID")
    return number


def content_type(request):
    return request.headers.get("content-type", "").partition(";")[0].strip().lower()


def wants_html(request):
    return "text/html" in request.headers.get("accept", "")


def page(request, template, *, status=200, headers=None, **context):
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={"member": getattr(request.state, "member", None),
                 "csrf_token": getattr(request.state, "csrf_token", None), **context},
        status_code=status,
        headers=headers,
    )


def success(data):
    return {"code": 0, "message": "ok", "data": data}
