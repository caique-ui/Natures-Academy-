"""
ragbot/web_scraper.py

Fetches and extracts text from web sources — both PDF and HTML.

Entry point:
    pages = fetch_web_source(root_url)
    # → [{"url": str, "title": str, "text": str, "content_hash": str}, ...]
"""
from __future__ import annotations

import hashlib
import io
import logging
import time
import tempfile
import os
import asyncio
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
# Helper function to execute async nodriver jobs inside sync environments
# ---------------------------------------------------------------------------
def _run_async(coro):
    """Safely runs an async coroutine inside Django/Celery synchronous workers."""
    try:
        import nodriver as uc
        return uc.loop().run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Nodriver browser instance wrapper factory
# ---------------------------------------------------------------------------
async def _make_selenium_driver(download_dir: str | None = None):
    """
    Creates an async nodriver browser instance.
    The display isolation is handled purely by Xvfb in the background.
    """
    try:
        import nodriver as uc
    except ImportError:
        raise ImportError("nodriver is required. Install with: pip install nodriver")

    browser = await uc.start(
        browser_args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--window-size=1920,1080",
        ]
    )
    
    if download_dir:
        main_page = await browser.get("about:blank")
        await main_page.send(
            uc.cdp.browser.set_download_behavior(
                behavior="allow",
                download_path=download_dir,
                events_enabled=True
            )
        )
    return browser


# ---------------------------------------------------------------------------
# Content-Type Detection Helpers
# ---------------------------------------------------------------------------
def _selenium_get_content_type(url: str) -> str:
    """Loads URL in a hidden virtual window to negotiate WAF and find Content-Type."""
    from xvfbwrapper import Xvfb

    async def _async_probe():
        driver = None
        try:
            driver = await _make_selenium_driver()
            page = await driver.get(url)
            await page.sleep(6)  # Give Cloudflare Turnstile time to clear
            
            content = await page.get_content()
            if "Verify you are human" in content or "cloudflare" in content.lower():
                try:
                    await page.cf_verify()
                    await page.sleep(4)
                except Exception:
                    pass

            src = await page.get_content() or ""
            if "%PDF" in src or "application/pdf" in src:
                return "application/pdf"
            return "text/html"
        finally:
            if driver is not None and not isinstance(driver, type(asyncio.Future)):
                try:
                    await driver.stop()
                except Exception:
                    pass

    vdisplay = Xvfb(width=1920, height=1080, colordepth=24)
    vdisplay.start()
    try:
        return _run_async(_async_probe())
    finally:
        vdisplay.stop()


def _detect_content_type(session: requests.Session, url: str) -> tuple[str, bool]:
    try:
        resp = session.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        time.sleep(10)  # brief pause to avoid triggering WAF on subsequent request
        print(f"Response text for {url}: {resp.text}")
        if resp.status_code == 200:
            return resp.headers.get("Content-Type", ""), False
        if resp.status_code in (405, 501):
            with session.get(url, stream=True, timeout=REQUEST_TIMEOUT) as r:
                if r.status_code == 200:
                    return r.headers.get("Content-Type", ""), False
    except requests.RequestException:
        pass

    logger.info(f"Plain requests blocked for {url}, probing via Nodriver + Xvfb.")
    ct = _selenium_get_content_type(url)
    return ct, True


# ---------------------------------------------------------------------------
# PDF Extraction Helpers
# ---------------------------------------------------------------------------
def _extract_pdf_pages(pdf_bytes: io.BytesIO, url: str) -> list[dict]:
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextContainer
    except ImportError:
        raise ImportError("pdfminer.six is required. Install with: pip install pdfminer.six")

    results: list[dict] = []
    try:
        for page_num, page_layout in enumerate(extract_pages(pdf_bytes), start=1):
            parts = [el.get_text() for el in page_layout if isinstance(el, LTTextContainer)]
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


def _fetch_pdf_via_selenium(url: str) -> list[dict]:
    """Download PDF securely via a background, virtual display browser session."""
    from xvfbwrapper import Xvfb
    
    with tempfile.TemporaryDirectory() as tmpdir:
        
        async def _async_download():
            driver = None
            try:
                driver = await _make_selenium_driver(download_dir=tmpdir)
                logger.info(f"Downloading PDF via Nodriver: {url}")
                page = await driver.get(url)
                await page.sleep(6)
                
                src = await page.get_content()
                if "Verify you are human" in src:
                    try:
                        await page.cf_verify()
                        await page.sleep(5)
                    except Exception:
                        pass

                pdf_path = None
                for _ in range(45):
                    files = [f for f in os.listdir(tmpdir) if f.endswith(".pdf")]
                    if files:
                        pdf_path = os.path.join(tmpdir, files[0])
                        break
                    await page.sleep(1)
                return pdf_path
            finally:
                if driver is not None and not isinstance(driver, type(asyncio.Future)):
                    try:
                        await driver.stop()
                    except Exception:
                        pass

        vdisplay = Xvfb(width=1920, height=1080, colordepth=24)
        vdisplay.start()
        try:
            pdf_file_path = _run_async(_async_download())
        finally:
            vdisplay.stop()

        if not pdf_file_path:
            logger.error(f"PDF did not download within timeout: {url}")
            return []

        with open(pdf_file_path, "rb") as f:
            return _extract_pdf_pages(io.BytesIO(f.read()), url)


# ---------------------------------------------------------------------------
# HTML Crawler Helpers
# ---------------------------------------------------------------------------
def _extract_html_text(soup) -> str:
    for selector in CONTENT_SELECTORS:
        el = soup.select_one(selector)
        if el:
            for tag in el.find_all(["nav", "header", "footer", "script", "style"]):
                tag.decompose()
            return el.get_text(separator="\n", strip=True)
    return soup.get_text(separator="\n", strip=True)


def _same_domain(url: str, root: str) -> bool:
    return urlparse(url).netloc == urlparse(root).netloc


def _should_skip(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in SKIP_EXTENSIONS)


async def _fetch_html_via_nodriver_page(driver, url: str) -> tuple[str, str, str]:
    from bs4 import BeautifulSoup
    page = await driver.get(url)
    await page.sleep(3)
    
    src = await page.get_content()
    if "Verify you are human" in src:
        try:
            await page.cf_verify()
            await page.sleep(4)
            src = await page.get_content()
        except Exception:
            pass

    final_url = page.url.split("#")[0].rstrip("/")
    soup = BeautifulSoup(src, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else url
    text  = _extract_html_text(soup)
    return title, text, final_url


def _crawl_html(
    session: requests.Session,
    root_url: str,
    max_pages: int = 300,
    use_selenium: bool = False,
    delay: float = 1.0,
) -> list[dict]:
    """
    BFS crawl utilizing Xvfb Virtual Framebuffer to transparently run a headed 
    browser engine hidden completely in the background.
    """
    try:
        from bs4 import BeautifulSoup
        from xvfbwrapper import Xvfb
    except ImportError:
        raise ImportError("beautifulsoup4 and xvfbwrapper are required.")

    root_prefix = root_url.rstrip("/")
    visited: set[str] = set()
    queue:   list[str] = [root_url]
    results: list[dict] = []

    async def _async_crawler_loop():
        driver = None
        try:
            if use_selenium:
                driver = await _make_selenium_driver()
                
            while queue and len(results) < max_pages:
                url = queue.pop(0).split("#")[0].rstrip("/")
                if not url or url in visited or _should_skip(url):
                    continue
                visited.add(url)

                html_source = None
                final_url   = url

                if use_selenium:
                    try:
                        title, text, final_url = await _fetch_html_via_nodriver_page(driver, url)
                        if text.strip():
                            results.append({
                                "url":          final_url,
                                "title":        title,
                                "text":         text,
                                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                            })
                        src = await driver.main_tab.get_content()
                        html_source = BeautifulSoup(src, "html.parser")
                    except Exception as exc:
                        logger.warning(f"Browser automation failed for {url}: {exc}")
                        continue
                else:
                    try:
                        resp = session.get(url, timeout=REQUEST_TIMEOUT)
                        if resp.status_code == 403:
                            logger.info(f"403 on {url}, retrying with browser fallback execution.")
                            if driver is None:
                                driver = await _make_selenium_driver()
                            try:
                                title, text, final_url = await _fetch_html_via_nodriver_page(driver, url)
                                if text.strip():
                                    results.append({
                                        "url":          final_url,
                                        "title":        title,
                                        "text":         text,
                                        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                                    })
                                src = await driver.main_tab.get_content()
                                html_source = BeautifulSoup(src, "html.parser")
                            except Exception as exc2:
                                logger.warning(f"Browser fallback failed for {url}: {exc2}")
                            continue

                        resp.raise_for_status()
                        ct = resp.headers.get("Content-Type", "")

                        if "application/pdf" in ct:
                            results.extend(_extract_pdf_pages(io.BytesIO(resp.content), url))
                            continue
                        if "text/html" not in ct:
                            continue

                        soup = BeautifulSoup(resp.text, "html.parser")
                        title = soup.title.string.strip() if soup.title and soup.title.string else url
                        text  = _extract_html_text(soup)
                        if text.strip():
                            results.append({
                                "url":          url,
                                "title":        title,
                                "text":         text,
                                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                            })
                        html_source = soup

                    except requests.RequestException as exc:
                        logger.warning(f"Scraper: failed to fetch {url}: {exc}")
                        continue

                if html_source:
                    for a in html_source.find_all("a", href=True):
                        href = urljoin(url, a["href"]).split("#")[0].rstrip("/")
                        if (
                            href
                            and href not in visited
                            and href.startswith(root_prefix)
                            and _same_domain(href, root_url)
                            and not _should_skip(href)
                        ):
                            queue.append(href)

                if driver:
                    await driver.main_tab.sleep(delay)
                else:
                    time.sleep(delay)
        finally:
            if driver is not None and not isinstance(driver, type(asyncio.Future)):
                try:
                    await driver.stop()
                except Exception:
                    pass

    vdisplay = Xvfb(width=1920, height=1080, colordepth=24)
    vdisplay.start()
    try:
        _run_async(_async_crawler_loop())
    finally:
        vdisplay.stop()

    logger.info(f"Crawled {len(results)} pages from {root_url}")
    return results


# ---------------------------------------------------------------------------
# Public Entry Points (Placed last so they see all functions defined above)
# ---------------------------------------------------------------------------
def fetch_web_source(root_url: str, max_pages: int = 300) -> list[dict]:
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)

    content_type, use_selenium = _detect_content_type(session, root_url)
    print(f"Content-Type for {root_url}: {content_type} (use_selenium={use_selenium})")
    if "application/pdf" in content_type:
        logger.info(f"Detected PDF: {root_url}")
        if use_selenium:
            return _fetch_pdf_via_selenium(root_url)
        return _fetch_pdf_via_requests(session, root_url)
    else:
        logger.info(f"Detected HTML: {root_url} (use_browser={use_selenium})")
        return _crawl_html(session, root_url, max_pages=max_pages, use_selenium=use_selenium)


def compute_web_fingerprint(pages: list[dict]) -> str:
    if not pages:
        return ""
    parts = sorted(f"{p['url']}:{p['content_hash']}" for p in pages)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()