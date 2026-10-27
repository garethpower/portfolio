#!/usr/bin/env python3
"""Convert an HTML help topic (e.g. MadCap Flare output) into a Docusaurus MDX file.

Usage:
    python scripts/html_to_docusaurus.py INPUT.htm [-o OUTPUT.mdx] [options]

Requires beautifulsoup4 (pip install beautifulsoup4).
"""

import argparse
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "body", "br", "dd", "details", "div",
    "dl", "dt", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hr", "li", "main", "ol", "p", "pre", "section", "summary", "table", "tbody",
    "td", "tfoot", "th", "thead", "tr", "ul",
}

DROP_TAGS = [
    "head", "script", "style", "noscript", "iframe", "form", "button", "svg",
    "template", "link", "meta", "nav",
]

# Navigation and chrome that MadCap Flare adds around the topic content.
DROP_SELECTORS = [
    "[class^=MCMiniToc]", "[class^=MCTopicToolbar]", "[class^=MCRelationships]",
    "[class^=MCWebHelpFramesetLink]", "img[src*=SkinSupport]",
]

# CSS class fragment -> Docusaurus admonition type. First match wins.
ADMONITIONS = [
    ("danger", "danger"), ("warning", "warning"), ("caution", "warning"),
    ("important", "info"), ("tip", "tip"), ("hint", "tip"), ("note", "note"),
    ("highlight", "note"),
]
ADMONITION_LABEL_RE = re.compile(
    r"^(\*\*|\*)?(note|tip|hint|important|warning|caution|danger)\s*:\s*(\*\*|\*)?\s*", re.I
)
BACK_TO_TOP_RE = re.compile(r"(go|back) to top|top of page", re.I)
DROPDOWN_RE = re.compile(r"^MCDropDown(_\d+)?$")


def escape(text):
    """Escape characters that Markdown or MDX would otherwise interpret."""
    text = text.replace("\\", "\\\\")
    text = re.sub(r"([*`\[\]{}])", r"\\\1", text)
    text = re.sub(r"(?<!\w)_|_(?!\w)", r"\\_", text)
    return text.replace("<", "&lt;")


def is_absolute(url):
    return urlparse(url).scheme in ("http", "https", "mailto", "ftp")


def quote_url(url):
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def is_block(node):
    return isinstance(node, Tag) and node.name in BLOCK_TAGS


def admonition_kind(node):
    classes = [c.lower() for c in node.get("class", [])]
    for key, kind in ADMONITIONS:
        if any(key in c for c in classes):
            return kind
    return None


class Converter:
    def __init__(self, source, base_url=None):
        self.source = source
        self.base_url = base_url
        self.in_cell = False
        self.images = {}  # local source path -> file name under img/
        self.warnings = []

    def convert(self, root):
        md = self.children(root)
        md = re.sub(r"[ \t]+\n", "\n", md)
        md = re.sub(r"\n{3,}", "\n\n", md).strip()
        # Flare brackets topics with decorative rules; at the top they would also clash with front matter.
        md = re.sub(r"^(---\n+)+|(\n+---)+$", "", md)
        return md + "\n"

    def children(self, node):
        return "".join(self.node(child) for child in node.children)

    def node(self, n):
        if isinstance(n, NavigableString):
            # Comments, doctypes and CDATA are NavigableString subclasses.
            return self.text(n) if type(n) is NavigableString else ""
        if not isinstance(n, Tag):
            return ""
        handler = getattr(self, "tag_" + n.name.replace(":", "_"), None)
        return handler(n) if handler else self.children(n)

    def text(self, n):
        s = re.sub(r"\s+", " ", str(n))
        prev, nxt = n.previous_sibling, n.next_sibling
        if is_block(prev) or (prev is None and is_block(n.parent)):
            s = s.lstrip()
        if is_block(nxt) or (nxt is None and is_block(n.parent)):
            s = s.rstrip()
        return escape(s)

    def block(self, s):
        s = s.strip()
        # Drop hard breaks left dangling at the end of a block.
        while s.endswith("\\") and not s.endswith("\\\\"):
            s = s[:-1].rstrip()
        return f"\n\n{s}\n\n" if s else ""

    def wrap(self, n, before, after=None):
        inner = self.children(n)
        core = inner.strip()
        if not core:
            return inner
        lead = inner[: len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()):]
        return f"{lead}{before}{core}{after or before}{trail}"

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    # Inline elements

    def tag_strong(self, n):
        return self.wrap(n, "**")

    tag_b = tag_strong

    def tag_em(self, n):
        return self.wrap(n, "*")

    tag_i = tag_em
    tag_cite = tag_em

    def tag_u(self, n):
        return self.wrap(n, "<u>", "</u>")

    def tag_s(self, n):
        return self.wrap(n, "~~")

    tag_del = tag_s
    tag_strike = tag_s

    def tag_code(self, n):
        code = re.sub(r"\s+", " ", n.get_text())
        if not code.strip():
            return code
        fence = "``" if "`" in code else "`"
        return f"{fence}{code}{fence}"

    tag_kbd = tag_code
    tag_samp = tag_code
    tag_tt = tag_code

    def tag_br(self, n):
        return "<br />" if self.in_cell else "\\\n"

    def tag_a(self, n):
        text = self.children(n).strip()
        href = n.get("href", "").strip()
        if not text or not href or href.lower().startswith("javascript:"):
            return text
        if href.startswith("#") or is_absolute(href):
            url = href
        elif self.base_url:
            url = urljoin(self.base_url, href)
        else:
            self.warn(f"Link to '{href}' converted to plain text (pass --base-url to keep links).")
            return text
        return f"[{text}]({quote_url(url)})"

    def tag_img(self, n):
        src = n.get("src", "").strip()
        if not src:
            return ""
        alt = escape(n.get("alt", "").strip())
        if is_absolute(src):
            url = src
        elif self.base_url:
            url = urljoin(self.base_url, src)
        else:
            path = (self.source.parent / unquote(urlparse(src).path)).resolve()
            self.images.setdefault(path, path.name)
            url = f"./img/{self.images[path]}"
        return f"![{alt}]({quote_url(url)})"

    # Block elements

    def container(self, n):
        kind = None if self.in_cell else admonition_kind(n)
        content = self.children(n).strip()
        if kind and content:
            content = ADMONITION_LABEL_RE.sub("", content, count=1)
            return self.block(f":::{kind}\n\n{content}\n\n:::")
        return self.block(content)

    tag_p = container
    tag_aside = container

    def tag_div(self, n):
        if any(DROPDOWN_RE.match(c) for c in n.get("class", [])):
            return self.dropdown(n)
        return self.container(n)

    def dropdown(self, n):
        """Flare drop-down (expandable section) -> <details>."""
        head = n.find(class_=re.compile(r"^MCDropDownHead"))
        body = n.find(class_=re.compile(r"^MCDropDownBody"))
        title = escape(" ".join(head.get_text().split())) if head else ""
        content = self.children(body).strip() if body else ""
        if self.in_cell:
            return self.block(f"**{title}**\n\n{content}")
        return self.block(f"<details>\n<summary>{title}</summary>\n\n{content}\n\n</details>")

    def heading(self, n, level):
        text = " ".join(self.children(n).split())
        if not text:
            return ""
        return self.block(f"**{text}**" if self.in_cell else f"{'#' * level} {text}")

    def tag_h1(self, n): return self.heading(n, 1)
    def tag_h2(self, n): return self.heading(n, 2)
    def tag_h3(self, n): return self.heading(n, 3)
    def tag_h4(self, n): return self.heading(n, 4)
    def tag_h5(self, n): return self.heading(n, 5)
    def tag_h6(self, n): return self.heading(n, 6)

    def tag_hr(self, n):
        return "" if self.in_cell else "\n\n---\n\n"

    def tag_blockquote(self, n):
        content = self.children(n).strip()
        if self.in_cell or not content:
            return self.block(content)
        return self.block("\n".join(f"> {line}".rstrip() for line in content.split("\n")))

    def tag_pre(self, n):
        code = n.get_text().strip("\n")
        if self.in_cell:
            return self.tag_code(n)
        classes = n.get("class", []) + (n.code.get("class", []) if n.code else [])
        lang = next((c.split("-", 1)[1] for c in classes if c.startswith(("language-", "lang-"))), "")
        fence = "~~~~" if "```" in code else "```"
        return self.block(f"{fence}{lang}\n{code}\n{fence}")

    def list(self, n, ordered):
        try:
            start = int(n.get("start", 1))
        except ValueError:
            start = 1
        items = []
        for i, li in enumerate(n.find_all("li", recursive=False)):
            marker = f"{start + i}." if ordered else ("•" if self.in_cell else "-")
            body = re.sub(r"\n{3,}", "\n\n", self.children(li).strip())
            body = body.replace("\n", "\n" + " " * (len(marker) + 1))
            items.append(f"{marker} {body}")
        return self.block("\n".join(items))

    def tag_ul(self, n): return self.list(n, ordered=False)
    def tag_ol(self, n): return self.list(n, ordered=True)

    def tag_dt(self, n):
        return self.block(self.wrap(n, "**"))

    def tag_dd(self, n):
        return self.block(self.children(n))

    def tag_table(self, n):
        rows = [tr for tr in n.find_all("tr") if tr.find_parent("table") is n]
        if self.in_cell:
            # Markdown can't nest tables; flatten to text.
            return self.block(" ".join(self.children(c) for tr in rows for c in tr.find_all(["td", "th"], recursive=False)))

        grid = []
        self.in_cell = True
        try:
            for tr in rows:
                cells = []
                for cell in tr.find_all(["td", "th"], recursive=False):
                    cells.append(self.cell(cell))
                    cells.extend([""] * (int(cell.get("colspan", 1) or 1) - 1))
                if any(cells):
                    grid.append(cells)
        finally:
            self.in_cell = False
        if not grid:
            return ""

        width = max(len(r) for r in grid)
        if width == 1:
            # Single-column tables are usually layout boxes, not data.
            return self.block("\n\n".join(r[0] for r in grid))
        grid = [r + [""] * (width - len(r)) for r in grid]
        lines = [self.row(grid[0]), self.row(["---"] * width)] + [self.row(r) for r in grid[1:]]
        return self.block("\n".join(lines))

    def cell(self, n):
        s = self.children(n).strip()
        s = re.sub(r"[ \t]*\n[ \t]*\n\s*", "<br /><br />", s)
        s = re.sub(r"[ \t]*\n[ \t]*", "<br />", s)
        return s.replace("|", "\\|")

    @staticmethod
    def row(cells):
        return "| " + " | ".join(cells) + " |"


def clean(root):
    for tag in root.find_all(DROP_TAGS):
        tag.decompose()
    # Flare puts breadcrumbs and the toolbar in a layout table above the topic.
    for crumbs in root.select("[class^=MCBreadcrumbsBox]"):
        if not crumbs.decomposed:
            (crumbs.find_parent("table") or crumbs).decompose()
    for selector in DROP_SELECTORS:
        for el in root.select(selector):
            if not el.decomposed:
                el.decompose()
    for a in root.find_all("a", href=re.compile(r"^\s*javascript:", re.I)):
        if BACK_TO_TOP_RE.search(a.get_text()):
            a.decompose()


def yaml_value(value):
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def main():
    parser = argparse.ArgumentParser(description="Convert an HTML page into a Docusaurus MDX file.")
    parser.add_argument("input", type=Path, help="HTML file to convert")
    parser.add_argument("-o", "--output", type=Path, help="output file (default: INPUT with an .mdx extension)")
    parser.add_argument("--selector", help="CSS selector for the content element (default: <body>)")
    parser.add_argument("--title", help="page title (default: the first <h1>, else <title>)")
    parser.add_argument("--sidebar-label", help="sidebar_label front matter (default: the title)")
    parser.add_argument("--sidebar-position", type=int, help="sidebar_position front matter")
    parser.add_argument(
        "--base-url",
        help="URL the HTML is published at. Relative links and images are made absolute against it. "
        "Without it, relative links become plain text and images are copied to img/ beside the output.",
    )
    args = parser.parse_args()

    output = args.output or args.input.with_suffix(".mdx")
    soup = BeautifulSoup(args.input.read_bytes(), "html.parser")

    root = soup.select_one(args.selector) if args.selector else (soup.body or soup)
    if root is None:
        sys.exit(f"error: selector '{args.selector}' matched nothing in {args.input}")
    clean(root)

    h1 = root.find("h1")
    title = args.title or (h1 and " ".join(h1.get_text().split())) or (
        soup.title and " ".join(soup.title.get_text().split())
    ) or args.input.stem

    converter = Converter(args.input, args.base_url)
    body = converter.convert(root)

    front = {}
    if not h1:
        # Docusaurus takes the page title from the first heading when there is one.
        front["title"] = title
    front["sidebar_label"] = args.sidebar_label or title
    if args.sidebar_position is not None:
        front["sidebar_position"] = args.sidebar_position
    header = "---\n" + "".join(f"{k}: {yaml_value(v)}\n" for k, v in front.items()) + "---\n\n"

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(header + body, encoding="utf-8")
    print(f"Wrote {output}")

    for src, name in converter.images.items():
        dest = output.parent / "img" / name
        if src.is_file():
            dest.parent.mkdir(exist_ok=True)
            shutil.copy2(src, dest)
        else:
            converter.warn(f"Image not found: {src} (copy it to {dest} or the build will fail).")
    for message in converter.warnings:
        print(f"warning: {message}", file=sys.stderr)


if __name__ == "__main__":
    main()
