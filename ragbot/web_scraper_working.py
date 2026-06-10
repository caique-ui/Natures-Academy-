"""
ragbot/web_scraper.py

Fetches and extracts text from web sources — both PDF and HTML.
Includes visual debugging (screenshots) and enhanced trace reporting.
"""
from __future__ import annotations

import hashlib
import io
import logging
import time
import tempfile
import os
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
REQUEST_TIMEOUT = 30

SKIP_EXTENSIONS = {".zip", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".mp4", ".mp3"}

CONTENT_SELECTORS = [
    "article", "main", ".legislation-content",
    "#content", ".content", "body",
]

# --- VISUAL DEBUGGING CONFIGURATION ---
DEBUG_DIR = os.path.join(os.getcwd(), "scraper_debug")
os.makedirs(DEBUG_DIR, exist_ok=True)


def _capture_screenshot(driver, name: str):
    """Saves a screenshot to help visualize what the headless browser sees."""
    try:
        timestamp = int(time.time())
        # Clean the name for filename safety
        safe_name = "".join(c for c in name if c.isalnum() or c in ("_", "-")).rstrip()
        filename = f"{timestamp}_{safe_name}.png"
        filepath = os.path.join(DEBUG_DIR, filename)
        
        # Set window size large enough to catch full content layout
        driver.set_window_size(1920, 1080)
        driver.save_screenshot(filepath)
        print(f"📸 Visual Debug: Screenshot captured -> {filepath}")
    except Exception as e:
        logger.warning(f"Failed to capture visual debug screenshot: {e}")


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def fetch_web_source(root_url: str, max_pages: int = 300) -> list[dict]:
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)

    print(f"🚀 Initializing check for: {root_url}")
    content_type, use_selenium = _detect_content_type(session, root_url)

    if "application/pdf" in content_type:
        print(f"📋 Target type verified: PDF")
        if use_selenium:
            return _fetch_pdf_via_selenium(root_url)
        return _fetch_pdf_via_requests(session, root_url)
    else:
        print(f"🌐 Target type verified: HTML (Using Selenium Bypass: {use_selenium})")
        return _crawl_html(session, root_url, max_pages=max_pages, use_selenium=use_selenium)


def compute_web_fingerprint(pages: list[dict]) -> str:
    if not pages:
        return ""
    parts = sorted(f"{p['url']}:{p['content_hash']}" for p in pages)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


# ---------------------------------------------------------------------------
# content-type detection
# ---------------------------------------------------------------------------

def _detect_content_type(session: requests.Session, url: str) -> tuple[str, bool]:
    try:
        resp = session.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if resp.status_code == 200:
            return resp.headers.get("Content-Type", ""), False
        if resp.status_code in (405, 501):
            with session.get(url, stream=True, timeout=REQUEST_TIMEOUT) as r:
                if r.status_code == 200:
                    return r.headers.get("Content-Type", ""), False
    except requests.RequestException:
        pass

    logger.info(f"Plain requests blocked for {url}, probing via Selenium.")
    print("🛡️  WAF/Block detected. Initializing headless Selenium browser to bypass...")
    ct = _selenium_get_content_type(url)
    return ct, True


def _selenium_get_content_type(url: str) -> str:
    driver = _make_selenium_driver()
    try:
        print(f"🔍 [Selenium] Loading URL to inspect content type: {url}")
        driver.get(url)
        time.sleep(10) # Let challenge pages resolve
        
        _capture_screenshot(driver, "content_type_probe")
        
        script = """
            const resp = await fetch(arguments[0], {method: 'HEAD'});
            return resp.headers.get('content-type') || '';
        """
        try:
            ct = driver.execute_async_script(
                "const cb = arguments[arguments.length-1];"
                "fetch(arguments[0], {method:'HEAD'})"
                ".then(r => cb(r.headers.get('content-type') || ''))"
                ".catch(() => cb(''))",
                url,
            )
            return ct or ""
        except Exception:
            src = driver.page_source or ""
            if "%PDF" in src or "application/pdf" in src:
                return "application/pdf"
            return "text/html"
    finally:
        driver.quit()


# ---------------------------------------------------------------------------
# Selenium driver factory
# ---------------------------------------------------------------------------

def _make_selenium_driver(download_dir: str | None = None):
    try:
        import undetected_chromedriver as uc
    except ImportError:
        raise ImportError("undetected-chromedriver is required.")

    options = uc.ChromeOptions()
    
    # 1. COMMENT OUT or REMOVE headless mode to allow the window to pop up
    options.add_argument("--headless=new") 
    
    # 2. Keep the rest of your stabilization flags
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    
    # 3. Explicitly pass a real user agent that matches your requests profile
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    if download_dir:
        prefs = {
            "download.default_directory": download_dir,
            "download.prompt_for_download": False,
            "plugins.always_open_pdf_externally": True,  
        }
        options.add_experimental_option("prefs", prefs)

    # Note: If it still triggers, let version_main auto-detect your local Chrome version
    driver = uc.Chrome(options=options, version_main=None)
    driver.set_page_load_timeout(300)
    return driver


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------

def _fetch_pdf_via_requests(session: requests.Session, url: str) -> list[dict]:
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error(f"Failed to download PDF {url}: {exc}")
        return []
    return _extract_pdf_pages(io.BytesIO(resp.content), url)


def _fetch_pdf_via_selenium(url: str) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmpdir:
        driver = _make_selenium_driver(download_dir=tmpdir)
        try:
            print(f"📥 [Selenium] Attempting stealth PDF download for: {url}")
            driver.get(url)
            
            # Wait up to 60s for the file to appear in temp dir
            pdf_path = None
            for i in range(60):
                files = [f for f in os.listdir(tmpdir) if f.endswith(".pdf")]
                if files:
                    pdf_path = os.path.join(tmpdir, files[0])
                    print(f"⏳ File found after {i} seconds. Path: {pdf_path}")
                    break
                
                # Check screenshot half-way if download stalls to see if stuck on an error page
                if i == 15:
                    _capture_screenshot(driver, "pdf_download_stalled")
                time.sleep(10)
        finally:
            driver.quit()

        if not pdf_path:
            print(f"❌ PDF download failed or timed out.")
            logger.error(f"PDF did not download within timeout: {url}")
            return []

        with open(pdf_path, "rb") as f:
            return _extract_pdf_pages(io.BytesIO(f.read()), url)


def _extract_pdf_pages(pdf_bytes: io.BytesIO, url: str) -> list[dict]:
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
        print(f"⚙️  Parsing binary PDF stream via pdfminer...")
        for page_num, page_layout in enumerate(extract_pages(pdf_bytes), start=1):
            parts = [
                el.get_text()
                for el in page_layout
                if isinstance(el, LTTextContainer)
            ]
            text = "\n".join(parts).strip()
            if not text:
                continue
            
            # Visualizing Text Read Extraction
            print(f"   📖 Successfully read PDF [Page {page_num}]. Extract Preview: {text[:80]}...")
            
            results.append({
                "url":          url,
                "title":        f"Page {page_num}",
                "text":         text,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            })
    except Exception as exc:
        logger.error(f"PDF parse error for {url}: {exc}", exc_info=True)

    print(f"✅ Finished PDF extraction. Total Pages with Content: {len(results)}")
    return results


# ---------------------------------------------------------------------------
# HTML crawler
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


def _fetch_html_via_selenium(driver, url: str) -> tuple[str, str, str]:
    from bs4 import BeautifulSoup
    print(f"🌍 [Selenium] Navigating to: {url}")
    driver.get(url)
    time.sleep(300)  # let JS render
    
    # Save a visual proof for debugging crawling sequence
    url_slug = urlparse(url).path.replace('/', '_')[-30:]
    _capture_screenshot(driver, f"crawl_{url_slug}")

    final_url = driver.current_url.split("#")[0].rstrip("/")
    soup = BeautifulSoup(driver.page_source, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else url
    text  = _extract_html_text(soup)
    
    print(f"   📖 Extracted HTML page title: '{title}'. Text Preview: {text[:80]}...")
    return title, text, final_url


def _crawl_html(
    session: requests.Session,
    root_url: str,
    max_pages: int = 300,
    use_selenium: bool = False,
    delay: float = 1.0,
) -> list[dict]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise ImportError(
            "beautifulsoup4 is required for HTML scraping.\n"
            "Install with:  pip install beautifulsoup4 lxml"
        )

    root_prefix = root_url.rstrip("/")
    visited: set[str] = set()
    queue:   list[str] = [root_url]
    results: list[dict] = []

    driver = _make_selenium_driver() if use_selenium else None

    try:
        while queue and len(results) < max_pages:
            url = queue.pop(0).split("#")[0].rstrip("/")
            if not url or url in visited or _should_skip(url):
                continue
            visited.add(url)

            html_source = None
            final_url   = url

            if use_selenium:
                try:
                    title, text, final_url = _fetch_html_via_selenium(driver, url)
                    if text.strip():
                        results.append({
                            "url":          final_url,
                            "title":        title,
                            "text":         text,
                            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                        })
                    soup = BeautifulSoup(driver.page_source, "html.parser")
                    html_source = soup
                except Exception as exc:
                    logger.warning(f"Selenium failed for {url}: {exc}")
                    continue
            else:
                try:
                    print(f"📡 [Requests] Fetching standard HTTP link: {url}")
                    resp = session.get(url, timeout=REQUEST_TIMEOUT)
                    if resp.status_code == 403:
                        print(f"🛑 403 Forbidden. Spinning up automated fallback browser...")
                        if driver is None:
                            driver = _make_selenium_driver()
                        try:
                            title, text, final_url = _fetch_html_via_selenium(driver, url)
                            if text.strip():
                                results.append({
                                    "url":          final_url,
                                    "title":        title,
                                    "text":         text,
                                    "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                                })
                            soup = BeautifulSoup(driver.page_source, "html.parser")
                            html_source = soup
                        except Exception as exc2:
                            logger.warning(f"Selenium fallback failed for {url}: {exc2}")
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

            time.sleep(delay)

    finally:
        if driver:
            print("🔌 Closing Selenium active browser session.")
            driver.quit()

    logger.info(f"Crawled {len(results)} pages from {root_url}")
    return results