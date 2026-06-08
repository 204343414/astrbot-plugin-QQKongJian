from __future__ import annotations

import asyncio
import random
import re
import time
from http.cookies import SimpleCookie
from typing import Any

import aiohttp
from astrbot.api import logger
from config import PluginConfig
from parser import QzoneParser


# ============================================================
# QQ空间登录会话：QzoneSession
# Source: core/qzone/session.py
# ============================================================

class QzoneSession:
    """QQ 登录上下文"""

    DOMAIN = "user.qzone.qq.com"

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self._ctx = None
        self._lock = asyncio.Lock()

    async def get_ctx(self):
        async with self._lock:
            if not self._ctx:
                self._ctx = await self.login(self.cfg.cookies_str)
            return self._ctx

    async def get_uin(self) -> int:
        ctx = await self.get_ctx()
        return ctx.uin

    async def get_nickname(self) -> str:
        ctx = await self.get_ctx()
        uin = str(ctx.uin)
        if not self.cfg.client:
            return uin
        try:
            info = await self.cfg.client.get_login_info()
            return info.get("nickname") or uin
        except Exception:
            return uin

    async def invalidate(self) -> None:
        async with self._lock:
            self._ctx = None

    async def _fetch_qzonetoken(self, uin: int, cookies: dict[str, str]) -> str:
        """从 QQ空间主页提取 qzonetoken。失败时返回空字符串，不阻断登录。"""
        url = f"https://user.qzone.qq.com/{uin}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            "Referer": "https://qzone.qq.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.cfg.timeout)) as client:
                async with client.get(url, headers=headers, cookies=cookies) as resp:
                    html = await resp.text()
            patterns = [
                r'window\.g_qzonetoken\s*=\s*"([^"]+)"',
                r"window\.g_qzonetoken\s*=\s*'([^']+)'",
                r'window\.g_qzonetoken\s*=\s*\(function\(\)\{\s*try\{return\s*"([^"]+)"',
                r"window\.g_qzonetoken\s*=\s*\(function\(\)\{\s*try\{return\s*'([^']+)'",
            ]
            for pattern in patterns:
                m = re.search(pattern, html, re.S)
                if m:
                    token = m.group(1).strip()
                    logger.info("qzonetoken 提取成功")
                    return token
            expr_match = re.search(r"window\.g_qzonetoken\s*=\s*\(function\(\)\{\s*try\{return\s*(.*?);\s*\}\s*catch", html, re.S)
            if expr_match:
                expr = expr_match.group(1).strip()
                logger.warning(f"检测到 g_qzonetoken 但暂不能求值，表达式片段：{expr[:120]}")
            elif "g_qzonetoken" in html:
                logger.warning("检测到 g_qzonetoken 但未能直接提取，可能是未知格式")
            else:
                snippet = html[:160].replace("\n", " ").replace("\r", " ")
                logger.warning(f"QQ空间主页未找到 g_qzonetoken，页面片段：{snippet!r}")
        except Exception as e:
            logger.warning(f"qzonetoken 提取失败：{e}")
        return ""

    async def login(self, cookies_str: str | None = None):
        logger.info("正在登录 QQ 空间")

        if not cookies_str:
            if not self.cfg.client:
                raise RuntimeError("CQHttp 实例不存在")
            cookies_str = (await self.cfg.client.get_cookies(domain=self.DOMAIN)).get(
                "cookies"
            )
            if not cookies_str:
                raise RuntimeError("获取 Cookie 失败")

            self.cfg.update_cookies(cookies_str)

        c = {k: v.value for k, v in SimpleCookie(cookies_str).items()}
        uin = int(c.get("uin", "0")[1:])
        if not uin:
            raise RuntimeError("Cookie 中缺少合法 uin")

        qzonetoken = await self._fetch_qzonetoken(uin, c)
        from model import QzoneContext
        self._ctx = QzoneContext(
            uin=uin,
            skey=c.get("skey", ""),
            p_skey=c.get("p_skey", ""),
            raw_cookies=c,
            qzonetoken=qzonetoken,
        )

        logger.info(f"登录成功，uin={uin}, qzonetoken={'有' if qzonetoken else '无'}")
        return self._ctx