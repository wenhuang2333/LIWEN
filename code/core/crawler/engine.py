import time
import re
import requests
from urllib.parse import urlparse, urljoin
from typing import Callable, Optional

from core.crawler.extractor import DataExtractor


_ALLOWED_SCHEMES = {"http", "https"}


class CrawlerEngine:
    """Headless crawl engine: HTTP fetching + data extraction.

    Design: the QWebEngineView is for visual configuration only.
    Actual crawling uses requests for speed and resource efficiency.
    """

    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    @staticmethod
    def validate_url(url: str) -> bool:
        """Reject dangerous URL schemes (file://, ftp://, etc.)."""
        if not url:
            return False
        try:
            parsed = urlparse(url)
            return parsed.scheme in _ALLOWED_SCHEMES
        except Exception:
            return False

    @staticmethod
    def fetch_page(url: str, headers: Optional[dict] = None,
                   proxy: Optional[str] = None,
                   timeout: int = 30) -> tuple:
        """Fetch a page. Returns (html_text, status_code)."""
        if not CrawlerEngine.validate_url(url):
            raise ValueError(f"不支持的 URL 协议: {url}")

        req_headers = dict(CrawlerEngine.DEFAULT_HEADERS)
        if headers:
            req_headers.update(headers)

        proxies = {"http": proxy, "https": proxy} if proxy else None

        resp = requests.get(url, headers=req_headers, proxies=proxies,
                           timeout=timeout, allow_redirects=True,
                           stream=True)
        chunks = []
        total = 0
        max_size = 10 * 1024 * 1024  # 10 MB limit
        for chunk in resp.iter_content(chunk_size=65536):
            chunks.append(chunk)
            total += len(chunk)
            if total > max_size:
                break
        resp.encoding = resp.apparent_encoding or "utf-8"
        raw = b"".join(chunks)
        return raw.decode(resp.encoding, errors="replace"), resp.status_code

    @staticmethod
    def _get_next_url(html_text: str, selector: str, current_url: str) -> Optional[str]:
        """Try to extract the next-page URL from a page using the selector.

        Checks: <a href>, element onclick, and parent <a> elements.
        """
        if not html_text or not selector:
            return None
        try:
            from lxml import html as lxml_html
            tree = lxml_html.fromstring(html_text)
            elements = tree.cssselect(selector)
            if not elements:
                return None

            el = elements[0]

            # 1. Direct <a> element
            if el.tag == "a":
                href = el.get("href", "")
                if href:
                    return urljoin(current_url, href)

            # 2. Child <a> inside the element
            links = el.cssselect("a")
            for link in links:
                href = link.get("href", "")
                if href:
                    return urljoin(current_url, href)

            # 3. onclick attribute with location/navigate
            onclick = el.get("onclick", "")
            if onclick:
                match = re.search(r"""location\s*[\.=\[]\s*['"]([^'"]+)['"]""", onclick)
                if match:
                    return urljoin(current_url, match.group(1))
                match = re.search(r"""navigate\s*\(\s*['"]([^'"]+)['"]""", onclick)
                if match:
                    return urljoin(current_url, match.group(1))

            # 4. Parent <a> element
            parent = el.getparent()
            if parent is not None and parent.tag == "a":
                href = parent.get("href", "")
                if href:
                    return urljoin(current_url, href)

        except Exception:
            pass
        return None

    @staticmethod
    def run_crawl(
        start_url: str,
        fields: list[dict],
        pagination: Optional[dict] = None,
        options: Optional[dict] = None,
        progress_cb: Optional[Callable[[int], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        data_row_cb: Optional[Callable[[dict], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> list[dict]:
        """Run a crawl job.

        Args:
            start_url: The starting URL (http/https only).
            fields: List of {"name", "selector", "type":"css"|"xpath"}.
            pagination: {
                "selector": str,        # next-page link selector
                "max_pages": int,
                "wait": float,
                "url_pattern": str,     # e.g. "https://x.com/page/{page}"
                "strategy": "link"|"pattern"|"scroll",
            }
            options: {
                "delay", "headers", "proxy", "timeout", "max_retries",
                "container_selector": str,
            }
            progress_cb: Called with 0-100 int.
            log_cb: Called with log message str.
            data_row_cb: Called with each extracted row dict for live preview.
            cancel_check: Callable returning True if cancelled.

        Returns:
            List of extracted data dicts.
        """

        def _log(msg):
            if log_cb:
                log_cb(msg)

        def _progress(pct):
            if progress_cb:
                progress_cb(pct)

        def _cancelled():
            if cancel_check:
                return cancel_check()
            return False

        if not CrawlerEngine.validate_url(start_url):
            raise ValueError(f"不支持的 URL 协议，仅支持 http/https: {start_url}")

        opts = options or {}
        pag = pagination or {}
        delay = float(opts.get("delay", 2.0))
        timeout = int(opts.get("timeout", 30))
        max_retries = int(opts.get("max_retries", 3))
        proxy = opts.get("proxy", "")
        headers = opts.get("headers") or {}
        max_pages = int(pag.get("max_pages", 1))
        wait_after_page = float(pag.get("wait", 0.5))
        next_selector = pag.get("selector", "")
        url_pattern = pag.get("url_pattern", "")
        strategy = pag.get("strategy", "link")
        container_selector = opts.get("container_selector", "")

        if not proxy:
            proxy = None
        if max_pages < 1:
            max_pages = 1

        all_data = []
        url = start_url

        _log(f"开始爬取: {url}")
        _progress(0)

        for page_num in range(1, max_pages + 1):
            if _cancelled():
                _log("爬取已取消")
                break

            # Generate URL for pattern strategy
            if strategy == "pattern" and url_pattern and page_num > 1:
                url = url_pattern.replace("{page}", str(page_num))
                url = url.replace("{p}", str(page_num))
                if not CrawlerEngine.validate_url(url):
                    _log(f"  URL 模式生成无效: {url}")
                    break

            _log(f"正在爬取第 {page_num} 页...")
            _progress(int(page_num / max_pages * 90))

            html_text = None
            for attempt in range(max_retries):
                if _cancelled():
                    break
                try:
                    html_text, status = CrawlerEngine.fetch_page(
                        url, headers, proxy, timeout)
                    _log(f"  HTTP {status} — {url}")
                    break
                except requests.RequestException as e:
                    _log(f"  尝试 {attempt + 1}/{max_retries} 失败: {e}")
                    if attempt < max_retries - 1 and not _cancelled():
                        time.sleep(1)
                    else:
                        _log(f"  页面 {page_num} 爬取失败，跳过")
                        html_text = None
                except ValueError as e:
                    _log(f"  URL 无效: {e}")
                    html_text = None
                    break

            if html_text:
                rows = DataExtractor.extract_fields(
                    html_text, fields, container_selector)
                _log(f"  提取到 {len(rows)} 条数据")
                for row in rows:
                    all_data.append(row)
                    if data_row_cb and not _cancelled():
                        data_row_cb(row)
            else:
                _log(f"  页面 {page_num} 无数据")

            # Inter-page delay
            if page_num < max_pages:
                total_wait = wait_after_page + delay
                for _ in range(int(total_wait * 10)):
                    if _cancelled():
                        break
                    time.sleep(0.1)

            if _cancelled():
                _log("爬取已取消")
                break

            # Determine next URL for non-pattern strategies
            if page_num < max_pages:
                if strategy == "pattern" and url_pattern:
                    continue  # URL is regenerated at top of loop

                if strategy == "link" and next_selector and html_text:
                    next_url = CrawlerEngine._get_next_url(
                        html_text, next_selector, url)
                    if next_url and CrawlerEngine.validate_url(next_url):
                        url = next_url
                        _log(f"  下一页: {url}")
                        continue
                    _log("  未找到下一页链接，停止翻页")
                    break

                # No pagination configured → stop after first page
                _log("  未配置翻页，停止")
                break

        _log(f"爬取完成，共 {len(all_data)} 条数据")
        _progress(100)
        return all_data
