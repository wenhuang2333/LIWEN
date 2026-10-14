from typing import Optional
from lxml import etree, html as lxml_html


class DataExtractor:
    """CSS/XPath-based data extraction from HTML documents.

    Supports two modes:
    - Container mode (recommended): find repeating item containers first,
      then scope sub-field extraction within each container. Each container
      produces one data row.
    - Flat mode (fallback): when no container selector, extract each field
      globally and zip values by index.
    """

    @staticmethod
    def _build_css_path(element) -> str:
        """Build a CSS selector path from lxml element to root."""
        parts = []
        el = element
        while el is not None:
            tag = el.tag if isinstance(el.tag, str) else ""
            if not tag:
                break
            el_id = el.get("id")
            if el_id:
                parts.append(f"#{el_id}")
                break
            classes = el.get("class", "").strip()
            if classes:
                tag_sel = f"{tag}.{classes.replace(' ', '.')}"
            else:
                tag_sel = tag
            parent = el.getparent()
            if parent is not None:
                siblings = [c for c in parent if isinstance(c.tag, str) and c.tag == tag]
                if len(siblings) > 1:
                    idx = siblings.index(el) + 1
                    tag_sel += f":nth-of-type({idx})"
            parts.append(tag_sel)
            el = parent
        return " > ".join(reversed(parts))

    @staticmethod
    def _build_xpath(element) -> str:
        """Build an XPath from lxml element to root."""
        parts = []
        el = element
        while el is not None:
            tag = el.tag if isinstance(el.tag, str) else ""
            if not tag:
                break
            el_id = el.get("id")
            if el_id:
                parts.append(f"//{tag}[@id='{el_id}']")
                break
            parent = el.getparent()
            if parent is not None:
                siblings = [c for c in parent if isinstance(c.tag, str) and c.tag == tag]
                if len(siblings) > 1:
                    idx = siblings.index(el) + 1
                    parts.append(f"{tag}[{idx}]")
                else:
                    parts.append(tag)
            else:
                parts.append(tag)
            el = parent
        return "/" + "/".join(reversed(parts))

    @staticmethod
    def generate_css_selector(element_info: dict) -> str:
        tag = element_info.get("tag", "")
        el_id = element_info.get("id", "")
        classes = element_info.get("classes", "")
        path = element_info.get("path", "")

        if el_id:
            return f"#{el_id}"
        if path:
            return path
        if classes:
            return f"{tag}.{classes.replace(' ', '.')}"
        return tag

    @staticmethod
    def generate_xpath(element_info: dict) -> str:
        tag = element_info.get("tag", "")
        el_id = element_info.get("id", "")
        classes = element_info.get("classes", "")
        xpath = element_info.get("xpath", "")

        if el_id:
            return f"//{tag}[@id='{el_id}']"
        if xpath:
            return xpath
        if classes:
            cls = classes.split()[0]
            return f"//{tag}[contains(@class, '{cls}')]"
        return f"//{tag}"

    @staticmethod
    def _select_elements(tree, selector: str, sel_type: str):
        """Run a CSS or XPath query against a tree/element. Returns list."""
        try:
            if sel_type == "xpath":
                return tree.xpath(selector)
            else:
                return tree.cssselect(selector)
        except Exception:
            return []

    @staticmethod
    def _get_text(el) -> str:
        """Extract text content from an element or string."""
        if isinstance(el, str):
            return el.strip()
        text = el.text_content().strip() if hasattr(el, "text_content") else str(el).strip()
        return text

    @staticmethod
    def extract_fields(html_text: str, field_selectors: list[dict],
                       container_selector: Optional[str] = None) -> list[dict]:
        """Extract data using field definitions.

        Each field: {"name": str, "selector": str, "type": "css"|"xpath"}

        When container_selector is provided (CSS only), the engine:
        1. Finds all container elements on the page
        2. Within each container, runs sub-field selectors
        3. Each container produces one row with its fields extracted

        Without container_selector, falls back to flat global extraction
        where each field is matched globally and values zipped by index.

        Returns list of dicts with keys matching field names.
        """
        if not html_text or not field_selectors:
            return []

        try:
            tree = lxml_html.fromstring(html_text)
        except Exception:
            return []

        # ── Container mode ──
        if container_selector:
            containers = DataExtractor._select_elements(
                tree, container_selector, "css")
            if not containers:
                return []

            rows = []
            for container in containers:
                row = {}
                for field in field_selectors:
                    name = field.get("name", "field")
                    selector = field.get("selector", "")
                    sel_type = field.get("type", "css")

                    elements = DataExtractor._select_elements(
                        container, selector, sel_type)
                    # For container mode: first match or attribute value
                    val = ""
                    if elements:
                        first = elements[0]
                        attr = field.get("attr", "")
                        if attr and hasattr(first, "get"):
                            val = (first.get(attr) or "").strip()
                        else:
                            val = DataExtractor._get_text(first)
                    row[name] = val
                rows.append(row)
            return rows

        # ── Flat mode (backward compat) ──
        rows = []
        max_len = 0
        field_values = {}

        for field in field_selectors:
            name = field.get("name", "field")
            selector = field.get("selector", "")
            sel_type = field.get("type", "css")

            elements = DataExtractor._select_elements(tree, selector, sel_type)
            attr = field.get("attr", "")

            values = []
            for el in elements:
                if attr and hasattr(el, "get"):
                    val = (el.get(attr) or "").strip()
                    if val:
                        values.append(val)
                else:
                    text = DataExtractor._get_text(el)
                    if text:
                        values.append(text)

            field_values[name] = values
            if len(values) > max_len:
                max_len = len(values)

        for i in range(max_len):
            row = {}
            for name in field_values:
                vals = field_values[name]
                row[name] = vals[i] if i < len(vals) else ""
            rows.append(row)

        return rows

    @staticmethod
    def validate_selector(html_text: str, selector: str,
                          sel_type: str = "css",
                          container_selector: Optional[str] = None) -> list[str]:
        """Return preview values for a single selector.

        When container_selector is provided, scopes the selector within
        the first container element for preview.
        """
        if not html_text or not selector:
            return []

        try:
            tree = lxml_html.fromstring(html_text)
        except Exception:
            return []

        if container_selector:
            containers = DataExtractor._select_elements(
                tree, container_selector, "css")
            if containers:
                tree = containers[0]

        elements = DataExtractor._select_elements(tree, selector, sel_type)

        values = []
        for el in elements[:20]:
            text = DataExtractor._get_text(el)
            if text:
                values.append(text)
        return values

    @staticmethod
    def validate_container(html_text: str, container_selector: str) -> int:
        """Return count of container elements matching the selector."""
        if not html_text or not container_selector:
            return 0
        try:
            tree = lxml_html.fromstring(html_text)
            containers = DataExtractor._select_elements(
                tree, container_selector, "css")
            return len(containers)
        except Exception:
            return 0
