"""
Scrape NICE guidance documents: HTG352, HTG313, HTG307, HTG20.

For each guidance ID:
  - BFS-crawls all HTML pages within /guidance/{id}/
  - Downloads evidence PDFs and extracts their text
  - Saves everything under output/{id}/

Usage:
    python scrape.py
"""

import asyncio
import io
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import pdfplumber
from bs4 import BeautifulSoup, NavigableString

GUIDANCE_IDS = ["htg352", "htg313", "htg307", "htg20"]
NICE_BASE = "https://www.nice.org.uk"
NICE_HOST = "www.nice.org.uk"
OUTPUT_DIR = Path(__file__).parent / "output"
CONCURRENCY = 6

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
}


# ---------------------------------------------------------------------------
# HTML → Markdown
# ---------------------------------------------------------------------------

def _inline(node) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    tag = node.name
    children = "".join(_inline(c) for c in node.children)
    if tag in ("strong", "b"):
        t = children.strip()
        return f"**{t}**" if t else ""
    if tag in ("em", "i"):
        t = children.strip()
        return f"*{t}*" if t else ""
    if tag == "code":
        return f"`{children}`"
    if tag == "a":
        href = node.get("href", "").strip()
        if href and not href.startswith("#"):
            href = urljoin(NICE_BASE, href)
        t = children.strip()
        return f"[{t}]({href})" if href and t else t
    if tag == "br":
        return "  \n"
    return children


def _block(node) -> str:
    if isinstance(node, NavigableString):
        t = str(node).strip()
        return t + "\n" if t else ""
    tag = node.name
    if not tag:
        return ""
    if tag in ("script", "style", "noscript", "nav", "header", "footer",
               "aside", "form", "button", "svg", "iframe", "input", "select"):
        return ""
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(tag[1])
        text = node.get_text(" ", strip=True)
        return f"\n{'#' * level} {text}\n\n" if text else ""
    if tag == "p":
        text = "".join(_inline(c) for c in node.children).strip()
        return f"{text}\n\n" if text else ""
    if tag == "ul":
        items = [
            f"- {re.sub(r'\\s*\\n\\s*', ' ', ''.join(_inline(c) for c in li.children).strip())}"
            for li in node.find_all("li", recursive=False)
        ]
        return "\n".join(items) + "\n\n" if items else ""
    if tag == "ol":
        items = [
            f"{i}. {re.sub(r'\\s*\\n\\s*', ' ', ''.join(_inline(c) for c in li.children).strip())}"
            for i, li in enumerate(node.find_all("li", recursive=False), 1)
        ]
        return "\n".join(items) + "\n\n" if items else ""
    if tag == "blockquote":
        inner = "".join(_block(c) for c in node.children).strip()
        return "\n".join(f"> {l}" for l in inner.splitlines()) + "\n\n"
    if tag == "pre":
        return f"```\n{node.get_text()}\n```\n\n"
    if tag == "table":
        return _table(node)
    if tag == "hr":
        return "\n---\n\n"
    if tag == "details":
        s = node.find("summary")
        heading = s.get_text(strip=True) if s else "Details"
        inner = "".join(
            _block(c) for c in node.children
            if not (hasattr(c, "name") and c.name == "summary")
        ).strip()
        return f"\n**{heading}**\n\n{inner}\n\n"
    return "".join(_block(c) for c in node.children)


def _table(tbl) -> str:
    rows = tbl.find_all("tr")
    if not rows:
        return ""
    out = []
    for r, tr in enumerate(rows):
        cells = tr.find_all(["th", "td"])
        cols = [c.get_text(" ", strip=True).replace("|", "\\|") for c in cells]
        if not cols:
            continue
        out.append("| " + " | ".join(cols) + " |")
        if r == 0:
            out.append("| " + " | ".join(["---"] * len(cols)) + " |")
    return "\n".join(out) + "\n\n" if out else ""


def html_to_markdown(html: str, source_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["nav", "header", "footer", "script", "style",
                               "noscript", "aside", "form"]):
        tag.decompose()
    for tag in soup.find_all(class_=re.compile(
            r"cookie|banner|breadcrumb|sidebar|share|print|skip|feedback|pagination",
            re.I)):
        tag.decompose()

    title = soup.title.get_text(strip=True) if soup.title else ""
    main = (soup.find("main") or soup.find("article")
            or soup.find("div", {"id": re.compile(r"content|main", re.I)})
            or soup.body or soup)

    md = f"# {title}\n\n" if title else ""
    md += f"**Source:** <{source_url}>\n\n---\n\n"
    md += _block(main)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


# ---------------------------------------------------------------------------
# PDF → Markdown
# ---------------------------------------------------------------------------

def pdf_bytes_to_markdown(data: bytes, source_url: str) -> str:
    lines = [f"**Source:** <{source_url}>", "", "---", ""]
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                text = page.extract_text(x_tolerance=2, y_tolerance=3)
                if text and text.strip():
                    lines.append(f"## Page {i}")
                    lines.append("")
                    lines.append(text.strip())
                    lines.append("")

                # Extract tables from the page
                tables = page.extract_tables()
                for table in tables:
                    if not table:
                        continue
                    header = table[0]
                    rows = table[1:]
                    cols = [str(c or "").replace("|", "\\|") for c in header]
                    lines.append("| " + " | ".join(cols) + " |")
                    lines.append("| " + " | ".join(["---"] * len(cols)) + " |")
                    for row in rows:
                        cells = [str(c or "").replace("|", "\\|") for c in row]
                        lines.append("| " + " | ".join(cells) + " |")
                    lines.append("")
    except Exception as e:
        lines.append(f"*PDF extraction error: {e}*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def normalise(url: str) -> str:
    p = urlparse(url)
    return p._replace(fragment="", query="").geturl().rstrip("/")


def subpath_slug(url: str, guidance_id: str) -> str:
    """Turn the URL path suffix after /guidance/{id} into a filename slug."""
    path = urlparse(url).path
    prefix = f"/guidance/{guidance_id}"
    suffix = path[len(prefix):].strip("/")
    if not suffix:
        return "index"
    slug = re.sub(r"[^a-z0-9]+", "-", suffix.lower()).strip("-")
    return slug or "index"


def is_within_guidance(url: str, guidance_id: str) -> bool:
    p = urlparse(url)
    return (p.netloc == NICE_HOST
            and p.path.lower().startswith(f"/guidance/{guidance_id}"))


def extract_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = normalise(urljoin(base_url, href))
        p = urlparse(full)
        if p.netloc != NICE_HOST:
            continue
        if full not in seen:
            seen.add(full)
            links.append(full)
    return links


# ---------------------------------------------------------------------------
# Crawl one guidance
# ---------------------------------------------------------------------------

async def fetch_resource(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str
) -> tuple[str, bytes, str]:
    """Fetch url, return (final_url, body_bytes, content_type)."""
    async with sem:
        resp = await client.get(url, headers=HEADERS, follow_redirects=True)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        return str(resp.url), resp.content, ct


async def crawl_guidance(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, guidance_id: str
) -> list[str]:
    """BFS-crawl all pages/PDFs within /guidance/{guidance_id}/."""
    root_url = f"{NICE_BASE}/guidance/{guidance_id}"
    visited: set[str] = {root_url}
    frontier: list[str] = [root_url]
    folder = OUTPUT_DIR / guidance_id
    folder.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []

    while frontier:
        tasks = [fetch_resource(client, sem, url) for url in frontier]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        next_frontier: list[str] = []

        for url, result in zip(frontier, results):
            if isinstance(result, Exception):
                print(f"  [{guidance_id}] ERROR {url}: {result}")
                continue

            final_url, body, ct = result
            final_url = normalise(final_url)

            # If redirected outside the guidance subtree, skip
            if not is_within_guidance(final_url, guidance_id):
                continue

            slug = subpath_slug(final_url, guidance_id)

            if "pdf" in ct.lower():
                md = pdf_bytes_to_markdown(body, final_url)
                filename = slug + ".md"
                (folder / filename).write_text(md, encoding="utf-8")
                print(f"  [{guidance_id}] PDF → {filename}")
                saved.append(final_url)

            elif "html" in ct.lower():
                html = body.decode("utf-8", errors="replace")
                md = html_to_markdown(html, final_url)
                filename = slug + ".md"
                (folder / filename).write_text(md, encoding="utf-8")
                print(f"  [{guidance_id}] HTML → {filename}")
                saved.append(final_url)

                # Discover new links within this guidance
                for link in extract_links(html, final_url):
                    norm = normalise(link)
                    if (norm not in visited
                            and is_within_guidance(norm, guidance_id)):
                        visited.add(norm)
                        next_frontier.append(norm)
            else:
                print(f"  [{guidance_id}] SKIP {ct[:40]} {final_url}")

        frontier = next_frontier

    return saved


# ---------------------------------------------------------------------------
# Top-level index
# ---------------------------------------------------------------------------

def write_top_index(per_guidance: dict[str, list[str]]) -> None:
    lines = ["# NICE Guidance Scrape\n"]
    total = sum(len(v) for v in per_guidance.values())
    lines.append(f"**Total pages/PDFs:** {total}\n")
    for gid, urls in per_guidance.items():
        lines.append(f"\n## {gid.upper()}\n")
        for url in urls:
            slug = subpath_slug(url, gid)
            lines.append(f"- [{url}]({gid}/{slug}.md)")
    (OUTPUT_DIR / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    ids = sys.argv[1:] if len(sys.argv) > 1 else GUIDANCE_IDS
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(CONCURRENCY)
    per_guidance: dict[str, list[str]] = {}

    async with httpx.AsyncClient(timeout=60.0) as client:
        for gid in ids:
            print(f"\n=== {gid.upper()} ===")
            saved = await crawl_guidance(client, sem, gid)
            per_guidance[gid] = saved
            print(f"  → {len(saved)} files saved")

    write_top_index(per_guidance)
    total = sum(len(v) for v in per_guidance.values())
    print(f"\nDone — {total} files in {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
