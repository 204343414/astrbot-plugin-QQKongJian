"""
投稿审核模块：PublishReview

功能：
1. 用户发说说前，LLM 审一遍内容
2. 有封号风险的内容不发
3. 累计超过 BAN_THRESHOLD 次违规 → 永久拉黑，不再允许投稿
4. 黑名单不写在明文规则里（用户无感知）
5. 审核通过的内容，发布时会自动加 "【来自 xxx 的投稿】" 标注
6. 同一用户 10 分钟内只能投 1 次（防刷）
"""

import json
import time
import asyncio
from pathlib import Path
from typing import Any

import aiosqlite

from astrbot.api import logger
from astrbot.core.provider.provider import Provider
from config import PluginConfig
from llm_action import LLMAction
from db import PostDB


class ReviewResult:
    """审核结果"""
    APPROVED = "approved"
    REJECTED = "rejected"
    BANNED = "banned"

    def __init__(self, status: str, reason: str = "", publish_text: str = "", strikes: int = 0):
        self.status = status
        self.reason = reason
        self.publish_text = publish_text
        self.strikes = strikes


class PublishReview:
    """
    投稿审核器：LLM 审 + 黑名单机制
    """

    BAN_THRESHOLD = 3
    COOLDOWN_SECONDS = 600  # 10 分钟冷却

    DEFAULT_REVIEW_PROMPT = (
        "你是一个QQ空间说说内容审核员。请判断以下投稿内容是否存在封号或违规风险。\n"
        "重点拦截：违法暴力、色情低俗、赌博毒品、诈骗钓鱼、恶意攻击、人肉网暴、广告引流、政治敏感等内容。\n"
        "普通日常、吐槽、游戏、学习、生活分享应通过；不要因为个别普通词过度拦截。\n\n"
        "待审核内容：\n{content}\n\n"
        "请只回答以下两种格式之一：\n"
        "通过\n"
        "或\n"
        "不通过|具体原因（简短一句话）"
    )

    def __init__(self, config: PluginConfig, db: PostDB, llm: LLMAction):
        self.cfg = config
        self.db = db
        self.llm = llm
        self._ban_db_path = config.data_dir / "publish_bans.db"
        self._strikes: dict[str, int] = {}
        self._last_submit_ts: dict[str, float] = {}  # user_id -> timestamp

    async def initialize(self):
        async with aiosqlite.connect(self._ban_db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS publish_strikes (
                    user_id TEXT PRIMARY KEY,
                    strike_count INTEGER NOT NULL DEFAULT 0,
                    last_strike_time INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT ''
                )
            """)
            await db.commit()
            async with db.execute("SELECT user_id, strike_count FROM publish_strikes") as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    self._strikes[row[0]] = row[1]
        logger.info(f"[PublishReview] 初始化完成，当前黑名单用户数: {self.get_banned_count()}")

    async def close(self):
        pass

    def is_banned(self, user_id: str) -> bool:
        return self._strikes.get(user_id, 0) >= self.BAN_THRESHOLD

    def get_strikes(self, user_id: str) -> int:
        return self._strikes.get(user_id, 0)

    def get_banned_count(self) -> int:
        return sum(1 for v in self._strikes.values() if v >= self.BAN_THRESHOLD)

    async def add_strike(self, user_id: str, reason: str = ""):
        current = self._strikes.get(user_id, 0) + 1
        self._strikes[user_id] = current
        async with aiosqlite.connect(self._ban_db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO publish_strikes (user_id, strike_count, last_strike_time, reason)
                   VALUES (?, ?, ?, ?)""",
                (user_id, current, int(time.time()), reason),
            )
            await db.commit()
        logger.warning(f"[PublishReview] 用户 {user_id} 违规 +1，累计 {current}/{self.BAN_THRESHOLD} 次，原因: {reason}")

    async def clear_strikes(self, user_id: str):
        if user_id in self._strikes:
            del self._strikes[user_id]
            async with aiosqlite.connect(self._ban_db_path) as db:
                await db.execute("DELETE FROM publish_strikes WHERE user_id = ?", (user_id,))
                await db.commit()
            logger.info(f"[PublishReview] 用户 {user_id} 违规记录已清除")

    async def submit(self, user_id: str, nickname: str, text: str, images: list[str] | None = None) -> ReviewResult:
        """
        投稿审核入口
        """
        # Step 1: 冷却检查
        now = time.time()
        last_ts = self._last_submit_ts.get(user_id, 0)
        if now - last_ts < self.COOLDOWN_SECONDS:
            remaining = int(self.COOLDOWN_SECONDS - (now - last_ts))
            logger.info(f"[PublishReview] 用户 {user_id} 投稿冷却中，剩余 {remaining} 秒")
            return ReviewResult(
                status=ReviewResult.REJECTED,
                reason=f"冷却中，请 {remaining} 秒后再试",
                strikes=self._strikes.get(user_id, 0),
            )

        # Step 2: 黑名单检查
        if self.is_banned(user_id):
            logger.info(f"[PublishReview] 用户 {user_id} 已被拉黑，拒绝投稿")
            return ReviewResult(
                status=ReviewResult.BANNED,
                reason="用户已被拉黑",
                strikes=self._strikes.get(user_id, 0),
            )

        # Step 3: LLM 审核
        # 旧版本这里先撞关键词库再调用 LLM；现在改为纯 LLM 审核，
        # 避免“加微信/转账”等普通语境被硬词库误杀。
        text_for_check = text or ""
        image_count = len(images or [])
        if image_count:
            text_for_check += f"\n[图片说说，共{image_count}张图片]"

        llm_result = await self._llm_review(
            content=text_for_check,
            text=text or "",
            image_count=image_count,
        )
        if not llm_result["approved"]:
            await self.add_strike(user_id, reason=f"LLM审核不通过: {llm_result.get('reason', '')}")
            self._last_submit_ts[user_id] = now
            logger.warning(f"[PublishReview] 用户 {user_id} 投稿LLM审核不通过: {llm_result.get('reason', '')}")
            return ReviewResult(
                status=ReviewResult.REJECTED,
                reason=f"内容审核未通过",
                strikes=self._strikes.get(user_id, 0),
            )

        # Step 4: 审核通过，加标注
        self._last_submit_ts[user_id] = now
        attribution_text = f"【来自 {nickname} 的投稿】\n\n{text}" if text else f"【来自 {nickname} 的投稿】"
        logger.info(f"[PublishReview] 用户 {user_id} 投稿审核通过")
        return ReviewResult(
            status=ReviewResult.APPROVED,
            publish_text=attribution_text,
        )

    def _render_review_prompt(self, *, content: str, text: str, image_count: int) -> str:
        template = (
            getattr(self.cfg.llm, "publish_review_prompt", "")
            or self.DEFAULT_REVIEW_PROMPT
        )
        original_template = template
        variables = {
            "content": content,
            "text": text,
            "image_count": str(image_count),
        }
        for key, value in variables.items():
            template = template.replace("{" + key + "}", value)
        if "{content}" not in original_template and content not in template:
            template += f"\n\n待审核内容：\n{content}"
        return template

    async def _llm_review(self, *, content: str, text: str = "", image_count: int = 0) -> dict[str, Any]:
        provider = (
            self.cfg.context.get_provider_by_id(self.cfg.llm.comment_provider_id)
            or self.cfg.context.get_using_provider()
        )
        if not isinstance(provider, Provider):
            logger.warning("[PublishReview] LLM 提供商不可用，审核不放行")
            return {"approved": False, "reason": "LLM不可用，无法完成审核"}

        prompt = self._render_review_prompt(content=content, text=text, image_count=image_count)

        try:
            response = await provider.text_chat(
                system_prompt="你是QQ空间内容审核员，严格审核用户投稿内容，存在封号风险的内容一律不通过。不要解释，不要多余的话。",
                prompt=prompt,
            )
            result_text = response.completion_text.strip()
            return self._parse_llm_result(result_text)
        except Exception as e:
            logger.error(f"[PublishReview] LLM 审核异常: {e}，审核不放行")
            return {"approved": False, "reason": f"LLM异常，无法完成审核: {e}"}

    def _parse_llm_result(self, text: str) -> dict[str, Any]:
        text = text.strip()
        text = LLMAction.strip_thinking(text)
        text = text.strip()

        if text.startswith("通过"):
            return {"approved": True, "reason": ""}

        if text.startswith("不通过"):
            parts = text.split("|", 1)
            reason = parts[1].strip() if len(parts) > 1 else "内容不符合规范"
            return {"approved": False, "reason": reason}

        if "通过" in text and "不" not in text[:4]:
            return {"approved": True, "reason": ""}
        if "不通过" in text or "拒绝" in text or "违规" in text:
            return {"approved": False, "reason": "内容审核不通过"}

        logger.warning(f"[PublishReview] LLM 返回结果无法解析: {text!r}，默认放行")
        return {"approved": True, "reason": "结果无法解析，默认放行"}
