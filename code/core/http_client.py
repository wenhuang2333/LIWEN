"""
Pure HTTP request function — extracted from HttpRequestWorker so the logic
can be used from CLI without any Qt dependency.
"""

import os
import time
import json as json_mod
from typing import Optional, Callable

import requests


def _cleanup_files(files):
    if files:
        for f in files.values():
            try:
                if hasattr(f, "close"):
                    f.close()
            except Exception:
                pass


def send_http_request(
    method: str,
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    body_config: Optional[dict] = None,
    timeout: int = 30,
    allow_redirects: bool = True,
    verify_ssl: bool = True,
    proxy: Optional[str] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    status_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    """Send an HTTP request and return the result dict.

    body_config = {
        "type": "none" | "form-data" | "x-www-form-urlencoded" | "raw" | "binary",
        "form_data": [{"key": ..., "value": ..., "row_type": "text"|"file"}],
        "raw_text": "...",
        "content_type": "json" | "xml" | "html" | "text",
        "file_path": "...",
    }

    Returns:
        {"status_code", "reason", "time_ms", "size",
         "headers", "cookies", "body_text", "url"}
    """
    body_config = body_config or {"type": "none"}
    params = params or {}
    headers = headers or {}

    def _cancelled():
        return cancel_check() if cancel_check else False

    if _cancelled():
        return {}

    if status_callback:
        status_callback("正在发送请求...")

    session = requests.Session()

    data = None
    json_data = None
    files = None

    body_type = body_config.get("type", "none")
    if body_type == "form-data":
        form_data = body_config.get("form_data", [])
        files = {}
        data = {}
        for row in form_data:
            key = row.get("key", "")
            val = row.get("value", "")
            row_type = row.get("row_type", "text")
            if row_type == "file" and val:
                try:
                    files[key] = (os.path.basename(val), open(val, "rb"))
                except Exception:
                    data[key] = val
            else:
                data[key] = val
        if not files:
            files = None
        if not data:
            data = None
    elif body_type == "x-www-form-urlencoded":
        form_data = body_config.get("form_data", [])
        data = {row.get("key", ""): row.get("value", "") for row in form_data}
    elif body_type == "raw":
        raw_text = body_config.get("raw_text", "")
        content_type = body_config.get("content_type", "text")
        if content_type == "json":
            headers.setdefault("Content-Type", "application/json")
            try:
                json_data = json_mod.loads(raw_text)
            except json_mod.JSONDecodeError:
                data = raw_text
        elif content_type == "xml":
            headers.setdefault("Content-Type", "application/xml")
            data = raw_text
        elif content_type == "html":
            headers.setdefault("Content-Type", "text/html")
            data = raw_text
        else:
            headers.setdefault("Content-Type", "text/plain")
            data = raw_text
    elif body_type == "binary":
        file_path = body_config.get("file_path", "")
        if file_path:
            try:
                data = open(file_path, "rb")
            except Exception as exc:
                raise ValueError(f"无法读取文件: {file_path}") from exc

    req = requests.Request(
        method=method.upper(),
        url=url,
        params=params if params else None,
        headers=headers if headers else None,
        data=data,
        json=json_data,
        files=files,
    )
    prepped = session.prepare_request(req)

    if _cancelled():
        _cleanup_files(files)
        return {}

    if proxy:
        session.proxies = {"http": proxy, "https": proxy}

    if status_callback:
        status_callback("等待响应...")
    t0 = time.time()
    resp = session.send(prepped, timeout=timeout,
                        allow_redirects=allow_redirects,
                        verify=verify_ssl)
    elapsed_ms = int((time.time() - t0) * 1000)

    _cleanup_files(files)

    if _cancelled():
        return {}

    try:
        body_text = resp.text
    except Exception:
        body_text = "(无法解码响应内容)"

    resp_headers = dict(resp.headers)
    cookies_list = []
    for cookie in resp.cookies:
        cookies_list.append({
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
        })

    return {
        "status_code": resp.status_code,
        "reason": resp.reason,
        "time_ms": elapsed_ms,
        "size": len(resp.content),
        "headers": resp_headers,
        "cookies": cookies_list,
        "body_text": body_text,
        "url": resp.url,
    }
