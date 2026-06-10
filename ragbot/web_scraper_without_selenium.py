"""
ragbot/web_scraper.py

Fetches and extracts text from web sources — PDF files and HTML sites.

Two entry points:

  1. fetch_web_source(root_url)
     For plain HTTP-accessible URLs (no Cloudflare).
     Auto-detects PDF vs HTML via Content-Type header.
     Used by the automated Celery task (scrape_web_source_task).

  2. index_local_file(file_path, source_url, label)
     For files you have already downloaded manually (e.g. CF-protected PDFs).
     Reads the file from disk, extracts text, returns the same page-dict list.
     Used by the `ingest_url` management command:
         python manage.py ingest_url --file /path/to/file.pdf \
             --url "https://legislation.nsw.gov.au/..." \
             --label "NSW Regs 2011-0653"

Both return:
    [{"url": str, "title": str, "text": str, "content_hash": str}, ...]
"""
from __future__ import annotations

import hashlib
import io
import logging
import mimetypes
import os
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.5",
}
REQUEST_TIMEOUT = 60

SKIP_EXTENSIONS = {".zip", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".mp4", ".mp3"}

CONTENT_SELECTORS = [
    "article", "main", ".legislation-content",
    "#content", ".content", "body",
]


# ---------------------------------------------------------------------------
# public entry point 1 — remote URL (no Cloudflare)
# ---------------------------------------------------------------------------

def fetch_web_source(root_url: str, max_pages: int = 300) -> list[dict]:
    """
    Fetch and extract text from a publicly accessible URL.
    Auto-detects PDF vs HTML via Content-Type.

    Will NOT work on Cloudflare-protected sites — use index_local_file() instead
    after downloading the file manually.
    """
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)

    content_type = _detect_content_type(session, root_url)
    logger.info(f"Detected content_type={content_type!r} for {root_url}")

    if "application/pdf" in content_type:
        return _fetch_pdf_via_requests(session, root_url)
    else:
        return _crawl_html(session, root_url, max_pages=max_pages)


# ---------------------------------------------------------------------------
# public entry point 2 — local file (manually downloaded)
# ---------------------------------------------------------------------------

def index_local_file(
    file_path: str,
    source_url: str,
    label: str = "",
) -> list[dict]:
    """
    Extract text from a file already on disk and return page dicts.

    Args:
        file_path:  Absolute path to the downloaded file (PDF or HTML).
        source_url: The canonical URL this file came from — stored as source_url
                    on SourceDocument so the chat UI can link back to it.
        label:      Optional human-friendly name (used in log messages only).

    Returns:
        List of page dicts: {url, title, text, content_hash}
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    # Detect type by extension first, then by reading the first bytes
    suffix = path.suffix.lower()
    if suffix == ".pdf" or _file_is_pdf(path):
        logger.info(f"Indexing local PDF: {file_path} (source: {source_url})")
        with open(path, "rb") as f:
            return _extract_pdf_pages(io.BytesIO(f.read()), source_url)
    elif suffix in (".html", ".htm"):
        logger.info(f"Indexing local HTML: {file_path} (source: {source_url})")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return _extract_html_pages(f.read(), source_url)
    else:
        # Try PDF first (some files have no extension), then treat as text
        if _file_is_pdf(path):
            with open(path, "rb") as f:
                return _extract_pdf_pages(io.BytesIO(f.read()), source_url)
        # Last resort: plain text
        logger.info(f"Indexing as plain text: {file_path}")
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            return []
        return [{
            "url":          source_url,
            "title":        label or path.name,
            "text":         text,
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        }]


def compute_web_fingerprint(pages: list[dict]) -> str:
    """SHA-256 of all (url, content_hash) pairs sorted by URL."""
    if not pages:
        return ""
    parts = sorted(f"{p['url']}:{p['content_hash']}" for p in pages)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


# ---------------------------------------------------------------------------
# content-type detection
# ---------------------------------------------------------------------------

def _detect_content_type(session: requests.Session, url: str) -> str:
    """Try HEAD then streaming GET to get Content-Type without downloading the body."""
    try:
        resp = session.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if resp.status_code == 200:
            return resp.headers.get("Content-Type", "")
        if resp.status_code in (405, 501):
            with session.get(url, stream=True, timeout=REQUEST_TIMEOUT) as r:
                if r.status_code == 200:
                    return r.headers.get("Content-Type", "")
    except requests.RequestException:
        pass
    return ""


def _file_is_pdf(path: Path) -> bool:
    """Check the PDF magic bytes (%PDF) at the start of the file."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"%PDF"
    except OSError:
        return False


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------

def _extract_pdf_pages(pdf_bytes: io.BytesIO, url: str) -> list[dict]:
    """
    Extract text page-by-page from a PDF using pdfminer.six.
    Each page becomes one dict so chunk sizes stay manageable.
    Requires:  pip install pdfminer.six
    """
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextContainer
    except ImportError:
        raise ImportError(
            "pdfminer.six is required for PDF extraction.\n"
            "Install with:  pip install pdfminer.six"
        )

    results: list[dict] = []
    try:
        for page_num, page_layout in enumerate(extract_pages(pdf_bytes), start=1):
            parts = [
                el.get_text()
                for el in page_layout
                if isinstance(el, LTTextContainer)
            ]
            text = "\n".join(parts).strip()
            if not text:
                continue
            results.append({
                "url":          url,
                "title":        f"Page {page_num}",
                "text":         text,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            })
    except Exception as exc:
        logger.error(f"PDF parse error for {url}: {exc}", exc_info=True)

    logger.info(f"Extracted {len(results)} pages from PDF: {url}")
    return results


def _fetch_pdf_via_requests(session: requests.Session, url: str) -> list[dict]:
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error(f"Failed to download PDF {url}: {exc}")
        return []
    return _extract_pdf_pages(io.BytesIO(resp.content), url)


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------

def _extract_html_text(soup) -> str:
    """Pull main content text from a BeautifulSoup tree."""
    for selector in CONTENT_SELECTORS:
        el = soup.select_one(selector)
        if el:
            for tag in el.find_all(["nav", "header", "footer", "script", "style"]):
                tag.decompose()
            return el.get_text(separator="\n", strip=True)
    return soup.get_text(separator="\n", strip=True)


def _extract_html_pages(html: str, url: str) -> list[dict]:
    """Parse a single HTML string and return one page dict."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise ImportError("beautifulsoup4 is required.  pip install beautifulsoup4 lxml")

    soup  = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else url
    text  = _extract_html_text(soup)
    if not text.strip():
        return []
    return [{
        "url":          url,
        "title":        title,
        "text":         text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
    }]


def _same_domain(url: str, root: str) -> bool:
    return urlparse(url).netloc == urlparse(root).netloc


def _should_skip(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in SKIP_EXTENSIONS)


def _crawl_html(
    session: requests.Session,
    root_url: str,
    max_pages: int = 300,
    delay: float = 1.0,
) -> list[dict]:
    """BFS crawl within the same URL prefix using plain requests."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise ImportError("beautifulsoup4 is required.  pip install beautifulsoup4 lxml")

    root_prefix = root_url.rstrip("/")
    visited: set[str] = set()
    queue:   list[str] = [root_url]
    results: list[dict] = []

    while queue and len(results) < max_pages:
        url = queue.pop(0).split("#")[0].rstrip("/")
        if not url or url in visited or _should_skip(url):
            continue
        visited.add(url)

        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "")
            if "application/pdf" in ct:
                results.extend(_extract_pdf_pages(io.BytesIO(resp.content), url))
                continue
            if "text/html" not in ct:
                continue
        except requests.RequestException as exc:
            logger.warning(f"Failed to fetch {url}: {exc}")
            continue

        soup  = BeautifulSoup(resp.text, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else url
        text  = _extract_html_text(soup)
        if text.strip():
            results.append({
                "url":          url,
                "title":        title,
                "text":         text,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            })

        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"]).split("#")[0].rstrip("/")
            if (
                href and href not in visited
                and href.startswith(root_prefix)
                and _same_domain(href, root_url)
                and not _should_skip(href)
            ):
                queue.append(href)

        time.sleep(delay)

    logger.info(f"Crawled {len(results)} pages from {root_url}")
    return results