from __future__ import annotations

import base64
import time
from typing import Any

import aiohttp
from astrbot.api import logger
from config import PluginConfig
from model import ApiResponse, Post, QzoneContext
from parser import QzoneParser
from utils import normalize_images
from qzone_session import QzoneSession


# ============================================================
# QQ空间HTTP客户端：QzoneHttpClient
# Source: core/qzone/client.py
# ============================================================

class QzoneHttpClient:
    def __init__(self, session: QzoneSession, config: PluginConfig):
        self.cfg = config
        self.session = session
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.cfg.timeout)
        )

    async def close(self):
        await self._session.close()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
        retry: int = 0,
    ) -> dict[str, Any]:
        ctx: QzoneContext = await self.session.get_ctx()
        req_headers = headers or ctx.headers()
        async with self._session.request(
            method,
            url,
            params=params,
            data=data,
            headers=req_headers,
            cookies=ctx.cookies(),
            timeout=timeout,
        ) as resp:
            text = await resp.text()
            status = resp.status
            final_url = str(resp.url)
            content_type = resp.headers.get("content-type", "")

        if not text.strip():
            if retry < 1:
                logger.warning(f"Qzone空响应，尝试刷新登录后重试：status={status}, url={final_url}")
                await self.session.invalidate()
                await self.session.login()
                return await self.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=headers,
                    timeout=timeout,
                    retry=retry + 1,
                )
            raise RuntimeError(
                "Qzone接口空响应："
                f"status={status}, content_type={content_type}, url={final_url}, "
                f"params={params}, qzonetoken={'有' if ctx.qzonetoken else '无'}, cookie_keys={sorted(ctx.cookies().keys())}"
            )

        try:
            parsed = QzoneParser.parse_response(text)
        except Exception as e:
            snippet = text[:300].replace("\n", " ")
            raise RuntimeError(
                f"Qzone响应解析失败：status={status}, content_type={content_type}, url={final_url}, "
                f"error={e}, snippet={snippet!r}"
            )

        if status in (401, 403) or parsed.get("code") == -3000:
            if retry >= 2:
                raise RuntimeError("登录失效，重试失败")

            logger.warning("登录失效，重新登录中")
            await self.session.login()
            return await self.request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                retry=retry + 1,
            )

        return parsed
