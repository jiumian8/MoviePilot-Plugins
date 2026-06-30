"""
HDHive Web 客户端（Cookie + 详情页 RSC + signedFetch）

用于替换 OpenAPI 搜索链路：
1. 访问 /tmdb/{movie|tv}/{tmdb_id}，从 NEXT_REDIRECT digest 解析最终详情页。
2. 访问 /movie/{page_id} 或 /tv/{page_id}，从 RSC groupData["115"] 提取资源。
3. 解锁时调用站内 signedFetch 接口 /api/customer/resources/{slug}/unlock。
"""
import base64
import gzip
import json
import queue
import random
import re
import secrets
import threading
import time
import urllib.parse
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from app.log import logger

from .hdhive import HDHiveOpenAPIError
from .hdhive_wasm_py import HDHivePythonSigner, HDHiveWasmUnavailable


class HDHiveWebClient:
    BASE_URL = "https://hdhive.com"
    LOGIN_PATH = "/login"
    LOGIN_ACTION_ID = "60ca8adfbbdff92bd6aef66cb04fa021f91f9bd3d8"
    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        username: str = "",
        password: str = "",
        cookie: str = "",
        proxy: Any = None,
        timeout: int = 30,
    ):
        self.username = username or ""
        self.password = password or ""
        self.cookie = cookie or ""
        self.timeout = timeout
        self._user_id = ""
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.UA})
        if isinstance(proxy, dict):
            self.proxies = proxy
        elif proxy:
            self.proxies = {"http": proxy, "https": proxy}
        else:
            self.proxies = None
        self._apply_cookie_string(self.cookie)

    @property
    def is_ready(self) -> bool:
        return bool(self.cookie or (self.username and self.password))

    def _apply_cookie_string(self, cookie: str) -> None:
        if not cookie:
            return
        for part in cookie.split(";"):
            if "=" not in part:
                continue
            name, value = part.strip().split("=", 1)
            if name:
                self.session.cookies.set(name, value, domain="hdhive.com")

    @staticmethod
    def _fix_mojibake(value: Any) -> Any:
        if isinstance(value, str):
            try:
                fixed = value.encode("latin1").decode("utf-8")
                if fixed.count("�") <= value.count("�") and any("\u4e00" <= ch <= "\u9fff" for ch in fixed):
                    return fixed
            except Exception:
                pass
            return value
        if isinstance(value, list):
            return [HDHiveWebClient._fix_mojibake(x) for x in value]
        if isinstance(value, dict):
            return {k: HDHiveWebClient._fix_mojibake(v) for k, v in value.items()}
        return value

    @classmethod
    def _decode_escaped_json(cls, fragment: str) -> Any:
        unescaped = fragment.encode("utf-8").decode("unicode_escape")
        return cls._fix_mojibake(json.loads(unescaped))

    @classmethod
    def _extract_balanced_escaped_json(cls, text: str, start: int) -> Any:
        if start < 0 or start >= len(text) or text[start] not in "[{":
            return None
        open_ch = text[start]
        close_ch = "}" if open_ch == "{" else "]"
        depth = 0
        in_raw_string = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_raw_string = not in_raw_string
                continue
            if not in_raw_string:
                if ch == open_ch:
                    depth += 1
                elif ch == close_ch:
                    depth -= 1
                    if depth == 0:
                        return cls._decode_escaped_json(text[start:i + 1])
        return None

    @staticmethod
    def _extract_redirect_page_id(page_html: str) -> Optional[str]:
        patterns = [
            r'NEXT_REDIRECT;replace;/(movie|tv)/([0-9a-f]{32});307;',
            r'NEXT_REDIRECT;replace;/(movie|tv)/([^;\\"\']+);307;',
            r'/(movie|tv)/([0-9a-f]{32})',
        ]
        for pat in patterns:
            m = re.search(pat, page_html)
            if m:
                return m.group(2)
        return None

    @classmethod
    def extract_115_resources_from_html(cls, page_html: str) -> List[Dict[str, Any]]:
        marker = '\\"groupData\\":'
        pos = page_html.find(marker)
        if pos == -1:
            return []
        start = page_html.find("{", pos + len(marker))
        group_data = cls._extract_balanced_escaped_json(page_html, start)
        if not isinstance(group_data, dict):
            return []
        resources = group_data.get("115") or []
        return resources if isinstance(resources, list) else []

    @classmethod
    def _extract_prop(cls, page_html: str, name: str) -> Any:
        key = r'\\"' + re.escape(name) + r'\\":'
        m = re.search(key, page_html)
        if not m:
            return None
        pos = m.end()
        while pos < len(page_html) and page_html[pos].isspace():
            pos += 1
        if pos >= len(page_html):
            return None
        if page_html[pos] == "{":
            return cls._extract_balanced_escaped_json(page_html, pos)
        m2 = re.match(r'(true|false|null|-?\d+(?:\.\d+)?|\\".*?\\")', page_html[pos:])
        if m2:
            return cls._decode_escaped_json(m2.group(1))
        return None

    def get_account_info(self) -> Dict[str, Any]:
        """读取 /manager/account 中的用户名和剩余积分。"""
        html = self._request_text("GET", self.BASE_URL + "/manager/account", headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": self.BASE_URL + "/",
        })
        user = self._extract_prop(html, "currentUser") or {}
        if not isinstance(user, dict):
            user = {}
        meta = user.get("user_meta") or {}
        username = user.get("username") or user.get("name") or user.get("email") or ""
        display_name = user.get("display_name") or user.get("nickname") or username
        user_id = user.get("id")
        if user_id is not None:
            self._user_id = str(user_id)
        points = meta.get("points")
        return {
            "id": str(user_id or ""),
            "username": str(username or ""),
            "display_name": str(display_name or ""),
            "points": points if points is not None else "",
        }

    def _request_text(self, method: str, url: str, **kwargs) -> str:
        self._ensure_login()
        headers = kwargs.pop("headers", {}) or {}
        headers.setdefault("User-Agent", self.UA)
        resp = self.session.request(method, url, headers=headers, proxies=self.proxies, timeout=self.timeout, **kwargs)
        resp.raise_for_status()
        return resp.text

    def _ensure_login(self) -> None:
        if self.session.cookies.get("token") and self.session.cookies.get("refresh_token"):
            return
        if self.cookie:
            # 已配置 cookie 但没有 token，继续让请求自己失败，避免无谓重登。
            return
        if not self.username or not self.password:
            return
        self.login()

    @classmethod
    def _make_next_router_state_tree(cls) -> str:
        tree = ["", {"children": ["(auth)", {"children": ["login", {"children": ["__PAGE__", {}]}]}]}, None, None, True]
        return urllib.parse.quote(json.dumps(tree, separators=(",", ":")))

    def _extract_login_action_id(self, login_html: str) -> Optional[str]:
        chunk_paths = re.findall(r'/_next/static/chunks/app/\(auth\)/login/page-[^"\\<>]+\.js', login_html)
        chunk_paths += re.findall(r'static/chunks/app/\(auth\)/login/page-[^"\\<>]+\.js', login_html)
        chunk_paths += re.findall(r'app/\(auth\)/login/page-[^"\\<>]+\.js', login_html)
        seen = set()
        for path in chunk_paths:
            if path in seen:
                continue
            seen.add(path)
            if path.startswith("/"):
                url = self.BASE_URL + path
            elif path.startswith("static/"):
                url = self.BASE_URL + "/_next/" + path
            elif path.startswith("app/"):
                url = self.BASE_URL + "/_next/static/chunks/" + path
            else:
                url = self.BASE_URL + "/" + path
            try:
                js = self.session.get(url, timeout=self.timeout, proxies=self.proxies).text
            except Exception:
                continue
            m = re.search(r'createServerReference\)?\("([0-9a-f]{40,})"[^;]{0,300}?"login"', js)
            if m:
                return m.group(1)
        return None

    def login(self) -> bool:
        login_url = self.BASE_URL + self.LOGIN_PATH
        resp = self.session.get(login_url, timeout=self.timeout, proxies=self.proxies, headers={"User-Agent": self.UA})
        resp.raise_for_status()
        action_id = self._extract_login_action_id(resp.text) or self.LOGIN_ACTION_ID
        payload = [{
            "username": self.username,
            "password": base64.b64encode(self.password.encode("utf-8")).decode("ascii"),
            "password_transport": "base64",
        }, "/"]
        headers = {
            "User-Agent": self.UA,
            "Accept": "text/x-component",
            "Content-Type": "text/plain;charset=UTF-8",
            "Next-Action": action_id,
            "Next-Router-State-Tree": self._make_next_router_state_tree(),
            "Origin": self.BASE_URL,
            "Referer": login_url,
        }
        r = self.session.post(login_url, data=json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), headers=headers, allow_redirects=False, timeout=self.timeout, proxies=self.proxies)
        ok = r.status_code in (200, 303) and bool(self.session.cookies.get("token")) and bool(self.session.cookies.get("refresh_token"))
        if not ok:
            logger.warning(f"HDHive Web 登录失败: HTTP {r.status_code}, {r.text[:200]}")
        return ok

    def query_resources(self, media_type: str, tmdb_id: Any) -> Dict[str, Any]:
        if media_type not in {"movie", "tv"}:
            raise HDHiveOpenAPIError("INVALID_MEDIA_TYPE", "media_type 只支持 movie/tv")
        tmdb_url = f"{self.BASE_URL}/tmdb/{media_type}/{tmdb_id}"
        logger.info(f"HDHive Web 访问 TMDB 跳转页: {tmdb_url}")
        html = self._request_text("GET", tmdb_url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": self.BASE_URL + "/",
        })
        page_id = self._extract_redirect_page_id(html)
        if not page_id:
            logger.info(f"HDHive Web 未解析到详情页 page_id: {tmdb_url}")
            return {"success": True, "data": []}
        detail_url = f"{self.BASE_URL}/{media_type}/{page_id}"
        logger.info(f"HDHive Web 访问详情页: {detail_url}")
        detail_html = self._request_text("GET", detail_url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": self.BASE_URL + "/",
        })
        resources = self.extract_115_resources_from_html(detail_html)
        for r in resources:
            if isinstance(r, dict):
                r.setdefault("pan_type", "115")
                r.setdefault("website", "115")
                r.setdefault("detail_url", detail_url)
        return {"success": True, "data": resources, "detail_url": detail_url, "page_id": page_id}

    @staticmethod
    def _readline_with_timeout(stream: Any, timeout: float, label: str) -> str:
        q: queue.Queue[str] = queue.Queue(maxsize=1)

        def worker() -> None:
            try:
                q.put(stream.readline())
            except Exception as exc:
                q.put(f"__ERROR__{exc}")

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        try:
            line = q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"等待 {label} 超时")
        if line.startswith("__ERROR__"):
            raise RuntimeError(line.replace("__ERROR__", "", 1))
        if not line:
            raise RuntimeError(f"{label} 没有返回数据")
        return line

    def _current_user_id(self) -> str:
        if self._user_id:
            return self._user_id
        try:
            html = self._request_text("GET", self.BASE_URL + "/manager/account", headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": self.BASE_URL + "/",
            })
            user = self._extract_prop(html, "currentUser") or {}
            if isinstance(user, dict) and user.get("id") is not None:
                self._user_id = str(user.get("id"))
                return self._user_id
        except Exception as e:
            logger.warning(f"HDHive 获取 currentUser.id 失败，将使用 0 签名: {e}")
        return "0"


    def _signed_request_json(self, method: str, path: str, referer: Optional[str] = None, body: Any = None) -> Any:
        wasm = Path(__file__).with_name("hdh_security_bg.wasm")
        if not wasm.exists():
            raise HDHiveOpenAPIError("MISSING_SIGNER", "缺少 hdh_security_bg.wasm")
        method = method.upper()
        body_bytes = b""
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                body_bytes = bytes(body)
            elif isinstance(body, str):
                body_bytes = body.encode("utf-8")
            else:
                body_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        self._ensure_login()
        try:
            signer = HDHivePythonSigner(str(wasm), user_agent=self.UA, languages="zh-CN,zh")
            init = signer.init()
        except HDHiveWasmUnavailable as e:
            raise HDHiveOpenAPIError(
                "WASMTIME_MISSING",
                "缺少 Python WASM 运行依赖 wasmtime",
                f"{e}；请确认 MoviePilot 已根据插件 requirements.txt 安装依赖。"
            )
        except Exception as e:
            raise HDHiveOpenAPIError("SIGNER_INIT_FAILED", "Python WASM 签名器初始化失败", str(e))
        try:
            handshake_body = json.dumps({
                "client_pub": init["client_pub"],
                "ua_fingerprint": init["ua_fingerprint"],
                "ts": init.get("ts") or int(time.time() * 1000),
            }, separators=(",", ":"))
            hs = self.session.post(
                self.BASE_URL + "/api/public/security/session/handshake",
                data=handshake_body.encode("utf-8"),
                headers={"User-Agent": self.UA, "Content-Type": "application/json", "Accept": "application/json", "Referer": referer or self.BASE_URL + "/"},
                proxies=self.proxies,
                timeout=15,
            ).json()
            if not hs.get("success") or not hs.get("data"):
                raise HDHiveOpenAPIError("HANDSHAKE_FAILED", "handshake失败", str(hs))
            cid = hs["data"]["cid"]
            server_pub = hs["data"]["server_pub"]
            split = urllib.parse.urlsplit(path)
            sig = signer.sign_after_handshake(
                {"cid": cid, "server_pub": server_pub, "kid": hs["data"].get("kid") or 1},
                method,
                split.path,
                body_bytes,
                self._current_user_id(),
            )
            headers = {
                "User-Agent": self.UA,
                "Accept": "application/json,text/plain,*/*",
                "Referer": referer or self.BASE_URL + "/",
                "X-HDH-Cid": cid,
                "X-HDH-TS": str(sig["ts"]),
                "X-HDH-Nonce": sig["nonce"],
                "X-HDH-Sig": sig["sig"],
                "X-HDH-Kid": "1",
            }
            data = None
            if body_bytes or method not in {"GET", "HEAD"}:
                data = body_bytes if body is not None else None
                if body is not None:
                    headers["Content-Type"] = "application/json"
            for attempt in range(3):
                resp = self.session.request(method, self.BASE_URL + path, data=data, headers=headers, proxies=self.proxies, timeout=25)
                try:
                    result = resp.json() if resp.text.strip() else None
                except Exception:
                    result = {"success": False, "message": resp.text[:500]}

                if resp.status_code == 429 or (isinstance(result, dict) and str(result.get("code", "")) == "429"):
                    base_wait = self._parse_retry_after_seconds(resp, result)
                    jitter = random.randint(10, 60)
                    wait_seconds = base_wait + jitter
                    if attempt >= 2:
                        raise HDHiveOpenAPIError("429", "频繁操作被暂时限制", f"多次等待后仍被 HDHive 限流，最后一次建议等待 {wait_seconds} 秒后再试", 429)
                    logger.warning(
                        f"HDHive signedFetch 触发频率限制，暂停 {wait_seconds} 秒后重试 "
                        f"(原限制 {base_wait} 秒 + 随机延迟 {jitter} 秒，attempt={attempt + 1}/3)"
                    )
                    time.sleep(wait_seconds)
                    continue

                if resp.status_code >= 400:
                    if isinstance(result, dict):
                        raise HDHiveOpenAPIError(str(result.get("code", resp.status_code)), str(result.get("message", "")), str(result.get("description", "")), resp.status_code)
                    raise HDHiveOpenAPIError(str(resp.status_code), "signedFetch 请求失败", str(result), resp.status_code)
                return result

            raise HDHiveOpenAPIError("429", "频繁操作被暂时限制", "多次等待后仍被 HDHive 限流，请稍后再试", 429)
        except HDHiveOpenAPIError:
            raise
        except Exception as e:
            raise HDHiveOpenAPIError("SIGNED_REQUEST_FAILED", "signedFetch 请求失败", str(e))

    @staticmethod
    def _parse_retry_after_seconds(resp: requests.Response, result: Any) -> int:
        candidates: List[str] = []
        retry_after = resp.headers.get("Retry-After") if resp is not None else None
        if retry_after:
            candidates.append(str(retry_after))
        if isinstance(result, dict):
            for key in ("retry_after", "retryAfter", "retry_after_seconds", "seconds", "description", "message"):
                value = result.get(key)
                if value is not None:
                    candidates.append(str(value))
        elif result is not None:
            candidates.append(str(result))
        for text in candidates:
            text = text.strip()
            if text.isdigit():
                return max(1, int(text))
            m = re.search(r"(\d+)\s*秒后重试", text)
            if m:
                return max(1, int(m.group(1)))
            m = re.search(r"after\s+(\d+)\s*seconds?", text, re.I)
            if m:
                return max(1, int(m.group(1)))
        return 60

    @staticmethod
    def _extract_share_url(data: Any) -> str:
        keys = {"full_url", "url", "link", "share_url", "share_link", "pan_url", "resource_url", "download_url", "shareUrl", "shareLink"}
        if isinstance(data, dict):
            for k, v in data.items():
                if k in keys and v:
                    return str(v)
                found = HDHiveWebClient._extract_share_url(v)
                if found:
                    return found
        elif isinstance(data, list):
            for x in data:
                found = HDHiveWebClient._extract_share_url(x)
                if found:
                    return found
        elif isinstance(data, str) and ("115.com" in data or "anxia.com" in data or "115cdn" in data):
            return data
        return ""

    def unlock_resource(self, slug: str) -> Dict[str, Any]:
        if not slug:
            raise HDHiveOpenAPIError("MISSING_SLUG", "缺少资源 slug")
        data = self._signed_request_json("POST", f"/api/customer/resources/{slug}/unlock", referer=self.BASE_URL + "/", body=None)
        share_url = self._extract_share_url(data)
        payload = data.get("data") if isinstance(data, dict) else data
        if isinstance(payload, dict):
            payload = dict(payload)
        else:
            payload = {"raw": payload}
        if share_url:
            payload["full_url"] = share_url
        return {"success": bool(isinstance(data, dict) and data.get("success", True)), "data": payload}
