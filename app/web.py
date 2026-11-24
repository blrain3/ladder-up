from pathlib import Path

from fastapi.templating import Jinja2Templates


APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=APP_DIR / "templates")


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
