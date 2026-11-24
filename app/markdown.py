"""禁用原始 HTML，只保留 HTTP(S) 链接和图片地址。"""

from urllib.parse import urlsplit

from markdown import Markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor


def safe_link(url: str) -> bool:
    if any(char.isspace() or ord(char) < 32 or char == "\\" for char in url):
        return False
    try:
        parsed = urlsplit(url)
        # 读取 port 可发现非法端口。IPv6 地址由 urlsplit 校验。
        return (
            parsed.scheme in {"http", "https"} and bool(parsed.hostname)
            and (parsed.port is None or parsed.port > 0)
            and "[" not in parsed.path and "]" not in parsed.path
        )
    except ValueError:
        return False


class _SafeLinkTreeprocessor(Treeprocessor):
    def run(self, root):
        for tag, attribute in (("a", "href"), ("img", "src")):
            for element in root.iter(tag):
                url = element.get(attribute)
                if url is not None and not safe_link(url):
                    del element.attrib[attribute]


class _SafeMarkdown(Extension):
    def extendMarkdown(self, md):
        md.preprocessors.deregister("html_block")
        md.inlinePatterns.deregister("html")
        md.treeprocessors.register(_SafeLinkTreeprocessor(md), "safe_links", priority=5)


def render(md: str) -> str:
    """将原始 HTML 作为文本展示，围栏代码由 Markdown 转义。"""
    if not isinstance(md, str):
        return ""
    return Markdown(
        extensions=["fenced_code", "nl2br", "sane_lists", _SafeMarkdown()],
        output_format="html",
    ).convert(md)
