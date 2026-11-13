# -*- coding: utf-8 -*-
"""Tiny HTTP helpers (stdlib only) with retry on transient errors."""
from __future__ import annotations
import json
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "AutoTubeStudio/1.0 (personal creator tool)"


class HttpError(RuntimeError):
    def __init__(self, status, message, body=""):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body


def request(method, url, *, params=None, headers=None, body=None, data=None, timeout=120, retries=2, raw=False):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    hdrs = {"User-Agent": UA}
    hdrs.update(headers or {})
    payload = data
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=payload, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as res:
                content = res.read()
                if raw:
                    return content
                return json.loads(content.decode("utf-8")) if content else {}
        except urllib.error.HTTPError as ex:
            text = ex.read().decode("utf-8", errors="replace")[:2000] if ex.fp else ""
            last = HttpError(ex.code, ex.reason, text)
            if ex.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(3 * (attempt + 1) ** 2)
                continue
            raise last
        except (urllib.error.URLError, TimeoutError, ConnectionError) as ex:
            last = HttpError(0, type(ex).__name__)
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            raise last
    raise last


def get_json(url, **kw):
    return request("GET", url, **kw)


def post_json(url, body, **kw):
    return request("POST", url, body=body, **kw)


def download(url, path, headers=None, timeout=300):
    content = request("GET", url, headers=headers, timeout=timeout, raw=True)
    with open(path, "wb") as f:
        f.write(content)
    return path


def parse_json_loose(text):
    """Parse model output that should be JSON (strips code fences, closes truncation)."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[s.find("{") if "{" in s else 0:]
    try:
        return json.loads(s)
    except ValueError:
        pass
    start = min([i for i in (s.find("{"), s.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("no JSON in model output")
    s = s[start:]
    end = max(s.rfind("}"), s.rfind("]"))
    if end > 0:
        try:
            return json.loads(s[:end + 1])
        except ValueError:
            pass
    # close truncated output
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    fixed = s + ('"' if in_str else "")
    fixed = fixed.rstrip().rstrip(",")
    return json.loads(fixed + "".join(reversed(stack)))
