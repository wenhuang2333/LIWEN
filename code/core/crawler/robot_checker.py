import re
from urllib.parse import urljoin, urlparse
from typing import Optional


_ROBOTS_TIMEOUT = 10


class SiteChecker:
    """Analyze a target site for crawl feasibility."""

    _ANTI_CRAWL_PATTERNS = [
        (re.compile(r"cloudflare", re.I), "Cloudflare"),
        (re.compile(r"captcha|recaptcha|hcaptcha|h-captcha", re.I), "CAPTCHA"),
        (re.compile(r"cf-request-header|cf-challenge", re.I), "Cloudflare Challenge"),
        (re.compile(r"browser.*check|checking.*browser", re.I), "Browser Check"),
        (re.compile(r"access.*denied|blocked|ip.*block", re.I), "Access Denied / IP Block"),
        (re.compile(r"distil|perimeterx|akamai.*bot|imperva", re.I), "Bot Detection"),
        (re.compile(r"ddos.*protect|ddos-guard", re.I), "DDoS Protection"),
    ]

    _CMS_PATTERNS = [
        (re.compile(r"wp-content|wp-includes|wordpress", re.I), "WordPress"),
        (re.compile(r"shopify", re.I), "Shopify"),
        (re.compile(r"__NEXT|next/", re.I), "Next.js"),
        (re.compile(r"react|ReactDOM|_reactRoot", re.I), "React"),
        (re.compile(r"vue|Vue\.createApp|v-bind|v-if|v-for", re.I), "Vue.js"),
        (re.compile(r"angular|ng-version|ng-app", re.I), "Angular"),
        (re.compile(r"magento|Mage\.", re.I), "Magento"),
        (re.compile(r"drupal|Drupal\.", re.I), "Drupal"),
        (re.compile(r"joomla", re.I), "Joomla"),
        (re.compile(r"wix", re.I), "Wix"),
        (re.compile(r"squarespace", re.I), "Squarespace"),
    ]

    _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/126.0.0.0 Safari/537.36")

    @staticmethod
    def _fetch(url: str, timeout: int = _ROBOTS_TIMEOUT) -> tuple:
        import requests
        try:
            resp = requests.get(url, headers={"User-Agent": SiteChecker._UA},
                                timeout=timeout, allow_redirects=True)
            return resp.text, dict(resp.headers), resp.status_code
        except Exception:
            return "", {}, 0

    @staticmethod
    def check_robots_txt(url: str) -> dict:
        result = {
            "url": url,
            "found": False,
            "allowed": True,
            "disallowed_paths": [],
            "crawl_delay": None,
            "sitemaps": [],
            "error": None,
        }
        try:
            parsed = urlparse(url)
            robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
            content, _, status = SiteChecker._fetch(robots_url)
            if status < 200 or status >= 400 or not content:
                result["error"] = f"robots.txt 不可达 (HTTP {status or '?'})"
                return result
            result["found"] = True
            result["allowed"] = SiteChecker._robots_allows(content, url)
            result["crawl_delay"] = SiteChecker._robots_crawl_delay(content)
            result["sitemaps"] = SiteChecker._robots_sitemaps(content)
        except Exception as e:
            result["error"] = str(e)
        return result

    @staticmethod
    def _robots_allows(content: str, url: str) -> bool:
        path = urlparse(url).path or "/"
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("disallow:"):
                rule = stripped.split(":", 1)[1].strip()
                if rule and path.startswith(rule):
                    return False
        return True

    @staticmethod
    def _robots_crawl_delay(content: str) -> Optional[float]:
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("crawl-delay:"):
                try:
                    return float(stripped.split(":", 1)[1].strip())
                except ValueError:
                    pass
        return None

    @staticmethod
    def _robots_sitemaps(content: str) -> list:
        maps = []
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("sitemap:"):
                u = stripped.split(":", 1)[1].strip()
                if u:
                    maps.append(u)
        return maps

    @staticmethod
    def detect_anti_crawl(html: str, response_headers: Optional[dict] = None) -> list[str]:
        signs = []
        for pattern, name in SiteChecker._ANTI_CRAWL_PATTERNS:
            if pattern.search(html):
                signs.append(name)

        if response_headers:
            server = response_headers.get("Server", "")
            if server and "cloudflare" in server.lower():
                if "Cloudflare" not in signs:
                    signs.append("Cloudflare (Server header)")

        return list(dict.fromkeys(signs))

    @staticmethod
    def detect_cms(html: str) -> Optional[str]:
        for pattern, name in SiteChecker._CMS_PATTERNS:
            if pattern.search(html):
                return name
        return None

    @staticmethod
    def check_site_feasibility(url: str, html: str = "",
                               response_headers: Optional[dict] = None,
                               status_code: int = 0) -> dict:
        robots = SiteChecker.check_robots_txt(url)
        has_js_framework = bool(re.search(
            r"react|vue|angular|__NEXT|_next|nuxt|svelte|ember",
            html, re.I)) if html else None
        has_login_form = bool(re.search(
            r'type=["\']?password["\']?|<form[^>]*login|<form[^>]*signin',
            html, re.I)) if html else None
        cms = SiteChecker.detect_cms(html) if html else None
        anti_crawl = SiteChecker.detect_anti_crawl(html, response_headers) if html else []

        return {
            "url": url,
            "status_code": status_code,
            "robots_ok": robots["allowed"],
            "robots_details": robots,
            "needs_js": has_js_framework,
            "has_captcha": "CAPTCHA" in anti_crawl,
            "has_login": has_login_form,
            "cms": cms,
            "anti_crawl_signs": anti_crawl,
            "ssl_valid": url.startswith("https"),
        }
