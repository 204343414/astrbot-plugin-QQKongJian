"""
投稿审核模块：PublishReview

功能：
1. 用户发说说前，LLM 审一遍内容
2. 有封号风险的内容不发
3. 审核状态分为三种：
   - 通过（approved）：内容安全，正常发布
   - 驳回（rejected）：内容不适合发布，但不记违规（含 LLM 调用失败）
   - 违规（violation）：内容涉及严重风险，记违规 1 次
4. 累计超过 BAN_THRESHOLD 次违规 → 永久拉黑
5. 黑名单不写在明文规则里（用户无感知）
6. 审核通过的内容，发布时会自动加 "【来自 xxx 的投稿】" 标注
7. 同一用户 10 分钟内只能投 1 次（防刷）
"""

from __future__ import annotations

import json
import re
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
    REJECTED = "rejected"     # 驳回：不发布，不记违规
    VIOLATION = "violation"   # 违规：不发布，记违规 +1
    BANNED = "banned"         # 已被拉黑
    ERROR = "error"           # 系统错误（LLM 超时/异常/provider 不可用），视为驳回，不记违规

    def __init__(self, status: str, reason: str = "", publish_text: str = "", strikes: int = 0):
        self.status = status
        self.reason = reason
        self.publish_text = publish_text
        self.strikes = strikes


class PublishReview:
    """
    投稿审核器：LLM 审 + 黑名单机制
    
    三种审核结果：
    - 通过：内容安全，发布
    - 驳回：内容不适合发布（质量低、真人照片、轻微问题），不记违规
    - 违规：内容涉及严重风险（政治/色情/暴力/诈骗），记违规，累计 3 次拉黑
    """

    BAN_THRESHOLD = 3
    COOLDOWN_SECONDS = 600  # 10 分钟冷却

    DEFAULT_REVIEW_PROMPT = (
        "你是一个QQ空间说说内容审核员。请判断以下投稿内容并给出审核结论。\n\n"
        "审核结论有三种：\n"
        "1. 通过：内容安全合规，可以发布\n"
        "2. 驳回：内容不适合发布（如质量低、不相关、轻微违规、真人照片但不涉及风险等），"
        "但不涉及严重封号风险\n"
        "3. 违规：内容涉及严重风险，可能导致bot被封号（政治敏感、暴力色情、赌博毒品、"
        "诈骗钓鱼、恶意攻击、人肉网暴、广告引流等）\n\n"
        "重点：\n"
        "- 普通日常、吐槽、游戏、学习、生活分享应通过\n"
        "- 真人照片、普通消息如果不涉及严重风险，应判为驳回而非违规\n"
        "- 只有明显可能导致bot被封号的内容才判为违规\n"
        "- 投稿来源显示名也属于将被发布的内容；如果显示名含政治敏感、违法、色情、"
        "广告引流等风险，判为违规\n\n"
        "投稿来源显示名：@{attribution_name}（原始昵称：{nickname}，用户ID：{user_id}）\n"
        "待审核内容：\n{content}\n\n"
        "请只回答以下三种格式之一：\n"
        "通过\n"
        "或\n"
        "驳回|具体原因（简短一句话）\n"
        "或\n"
        "违规|具体原因（简短一句话）"
    )

    _UNSAFE_ATTRIBUTION_PATTERNS = [
        "加微信", "加vx", "加v", "微信", "vx", "v信", "加qq", "加群",
        "转账", "收款", "付款", "付费", "刷单", "兼职", "赚钱", "约炮",
        "裸", "黄", "色情", "福利", "外围", "博彩", "赌博", "代充", "广告",
        "http://", "https://", "www.", ".com", ".cn", "t.me",
    ]

    @staticmethod
    def _safe_attribution_name(user_id: str, nickname: str) -> str:
        """
        投稿来源会被发到 QQ 空间正文里，不能直接信任群昵称。
        昵称如果含广告/引流/擦边/链接等风险词，就退回不可引流的短标识。
        """
        raw = str(nickname or "").strip()
        raw = re.sub(r"\[CQ:[^\]]+\]", "", raw)
        raw = re.sub(r"[\r\n\t]+", " ", raw)
        raw = re.sub(r"\s+", " ", raw).strip()
        raw = raw.strip("@｜|:：,，;；[]【】()（）<>《》")
        compact = re.sub(r"\s+", "", raw).lower()

        uid = re.sub(r"\D", "", str(user_id or ""))
        fallback = f"用户{uid[-4:]}" if len(uid) >= 4 else "投稿人"

        if not raw or len(raw) > 24:
            return fallback
        if any(p in compact for p in PublishReview._UNSAFE_ATTRIBUTION_PATTERNS):
            return fallback
        if re.search(r"(?:\d[ -]?){5,}", raw):
            return fallback
        if re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", raw):
            return fallback

        # 保留常见昵称字符，去掉容易构造链接/命令的符号。
        safe = re.sub(r"[^0-9A-Za-z_\- \u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af·・]", "", raw)
        safe = re.sub(r"\s+", " ", safe).strip()[:16]
        return safe or fallback

    @staticmethod
    def build_attribution_text(user_id: str, nickname: str, text: str) -> str:
        # 投稿归属用 QQ空间可点击 @好友标记：@{uin:投稿人QQ号,nick:安全昵称}
        # 这样发到空间后，"【来自 @某人 的投稿】"里的名字是蓝色超链接，
        # 别人点一下就能跳到投稿人主页，找到具体是谁。
        # 昵称仍经 _safe_attribution_name 清洗（防广告/引流/链接昵称被带进空间，
        # 且已滤掉 {} 和逗号，不会破坏标记结构）。
        safe_name = PublishReview._safe_attribution_name(user_id, nickname)
        uid = re.sub(r"\D", "", str(user_id or ""))
        if uid:
            attribution = "@{uin:%s,nick:%s}" % (uid, safe_name)
        else:
            # 拿不到有效 QQ 号时退回纯文本，至少不丢信息
            attribution = f"@{safe_name}"
        prefix = f"【来自 {attribution} 的投稿】"
        return f"{prefix}\n\n{text}" if text else prefix

    def __init__(self, config: PluginConfig, db: PostDB, llm: LLMAction):
        self.cfg = config
        self.db = db
        self.llm = llm
        self._ban_db_path = config.data_dir / "publish_bans.db"
        self._strikes: dict[str, int] = {}
        self._strike_reasons: dict[str, str] = {}
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
            async with db.execute("SELECT user_id, strike_count, reason FROM publish_strikes") as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    user_id = row[0]
                    strike_count = row[1]
                    reason = row[2]
                    self._strikes[user_id] = strike_count
                    if reason:
                        self._strike_reasons[user_id] = reason
        logger.info(f"[PublishReview] 初始化完成，当前黑名单用户数: {self.get_banned_count()}")

    async def close(self):
        pass

    def is_banned(self, user_id: str) -> bool:
        return self._strikes.get(user_id, 0) >= self.BAN_THRESHOLD

    def get_strikes(self, user_id: str) -> int:
        return self._strikes.get(user_id, 0)

    def get_banned_count(self) -> int:
        return sum(1 for v in self._strikes.values() if v >= self.BAN_THRESHOLD)

    def get_banned_users(self) -> list[dict[str, Any]]:
        """获取所有被封禁的用户列表"""
        banned = []
        for user_id, strikes in self._strikes.items():
            if strikes >= self.BAN_THRESHOLD:
                banned.append({
                    "user_id": user_id,
                    "strikes": strikes,
                    "reason": self._strike_reasons.get(user_id, ""),
                })
        return banned

    def get_all_strike_records(self) -> list[dict[str, Any]]:
        """获取所有有违规记录的用户（包括未满BAN_THRESHOLD的）"""
        records = []
        for user_id, strikes in sorted(self._strikes.items(), key=lambda x: x[1], reverse=True):
            records.append({
                "user_id": user_id,
                "strikes": strikes,
                "reason": self._strike_reasons.get(user_id, ""),
                "banned": strikes >= self.BAN_THRESHOLD,
            })
        return records

    async def add_strike(self, user_id: str, reason: str = ""):
        current = self._strikes.get(user_id, 0) + 1
        self._strikes[user_id] = current
        self._strike_reasons[user_id] = reason
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
        if user_id in self._strike_reasons:
            del self._strike_reasons[user_id]
        async with aiosqlite.connect(self._ban_db_path) as db:
            await db.execute("DELETE FROM publish_strikes WHERE user_id = ?", (user_id,))
            await db.commit()
        logger.info(f"[PublishReview] 用户 {user_id} 违规记录已清除")

    async def clear_all_strikes(self) -> int:
        """清空所有用户的违规记录。返回被清空的用户数。"""
        count = len(self._strikes)
        self._strikes.clear()
        self._strike_reasons.clear()
        async with aiosqlite.connect(self._ban_db_path) as db:
            await db.execute("DELETE FROM publish_strikes")
            await db.commit()
        logger.info(f"[PublishReview] 已清空所有 {count} 个用户的违规记录")
        return count

    async def submit(self, user_id: str, nickname: str, text: str, images: list[str] | None = None) -> ReviewResult:
        """
        投稿审核入口
        
        返回 ReviewResult，status 可能为：
        - APPROVED: 审核通过，可以发布
        - REJECTED: 审核驳回（内容不合规或LLM错误），不记违规
        - VIOLATION: 审核发现严重违规，记违规 +1
        - BANNED: 用户已被拉黑
        - ERROR: 系统错误（LLM 调用失败），视为驳回，不记违规
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
        text_for_check = text or ""
        image_count = len(images or [])
        if image_count:
            text_for_check += f"\n[图片说说，共{image_count}张图片]"

        attribution_name = self._safe_attribution_name(user_id, nickname)
        llm_result = await self._llm_review(
            content=text_for_check,
            text=text or "",
            image_count=image_count,
            images=images or [],
            user_id=user_id,
            nickname=nickname,
            attribution_name=attribution_name,
        )

        # ⚠️ 关键修复：区分"系统错误"和"内容违规"。
        # 大模型超时 / 异常 / provider 不可用属于系统错误（error=True），
        # 这种情况下：不记违规、不计入拉黑阈值、不进冷却（让用户能立刻重试），
        # 只返回 ERROR 让上层提示"稍后再试"。
        if llm_result.get("error"):
            logger.warning(
                f"[PublishReview] 用户 {user_id} 投稿审核未完成（系统问题，不记违规）: "
                f"{llm_result.get('reason', '')}"
            )
            return ReviewResult(
                status=ReviewResult.ERROR,
                reason=llm_result.get("reason", "审核服务暂时不可用，请稍后再试"),
                strikes=self._strikes.get(user_id, 0),
            )

        # Step 4: 根据审核结果处理
        status = llm_result.get("status", "approved")

        if status == "violation":
            # 严重违规：记违规 +1，检查是否达到拉黑阈值
            reason = llm_result.get("reason", "内容涉及严重违规")
            await self.add_strike(user_id, reason=f"LLM审核违规: {reason}")
            self._last_submit_ts[user_id] = now
            logger.warning(f"[PublishReview] 用户 {user_id} 投稿LLM审核违规: {reason}")
            return ReviewResult(
                status=ReviewResult.VIOLATION,
                reason=reason,
                strikes=self._strikes.get(user_id, 0),
            )

        if status == "rejected":
            # 驳回：不发布，但不记违规
            reason = llm_result.get("reason", "内容不符合规范")
            self._last_submit_ts[user_id] = now
            logger.info(f"[PublishReview] 用户 {user_id} 投稿LLM审核驳回: {reason}")
            return ReviewResult(
                status=ReviewResult.REJECTED,
                reason=reason,
                strikes=self._strikes.get(user_id, 0),
            )

        # Step 5: 审核通过，加标注
        self._last_submit_ts[user_id] = now
        attribution_text = self.build_attribution_text(user_id, nickname, text)
        logger.info(f"[PublishReview] 用户 {user_id} 投稿审核通过")
        return ReviewResult(
            status=ReviewResult.APPROVED,
            publish_text=attribution_text,
        )

    def _render_review_prompt(self, *, content: str, text: str, image_count: int,
                              user_id: str = "", nickname: str = "",
                              attribution_name: str = "") -> str:
        template = (
            getattr(self.cfg.llm, "publish_review_prompt", "")
            or self.DEFAULT_REVIEW_PROMPT
        )
        original_template = template
        variables = {
            "content": content,
            "text": text,
            "image_count": str(image_count),
            "user_id": str(user_id or ""),
            "nickname": str(nickname or ""),
            "attribution_name": str(attribution_name or ""),
        }
        for key, value in variables.items():
            template = template.replace("{" + key + "}", value)
        if "{content}" not in original_template and content not in template:
            template += f"\n\n待审核内容：\n{content}"
        if "{attribution_name}" not in original_template and attribution_name:
            template += f"\n\n投稿来源显示名：@{attribution_name}（原始昵称：{nickname}，用户ID：{user_id}）"
        return template

    async def _llm_review(self, *, content: str, text: str = "", image_count: int = 0,
                          images: list[str] | None = None, user_id: str = "",
                          nickname: str = "", attribution_name: str = "") -> dict[str, Any]:
        provider = (
            self.cfg.context.get_provider_by_id(self.cfg.llm.comment_provider_id)
            or self.cfg.context.get_using_provider()
        )
        if not isinstance(provider, Provider):
            # provider 不可用属于"系统侧问题"，不是用户内容违规。
            # 标记 error=True，让上层只拒绝、不记违规、不拉黑。
            logger.warning("[PublishReview] LLM 提供商不可用，无法完成审核（不记违规）")
            return {"status": "rejected", "error": True, "reason": "LLM不可用，无法完成审核"}

        prompt = self._render_review_prompt(
            content=content, text=text, image_count=image_count,
            user_id=user_id, nickname=nickname, attribution_name=attribution_name,
        )
        image_inputs: list[str] = []
        if images:
            prompt += (
                "\n\n图片审核补充规则：请认真识别图片内容。"
                "严重违规（色情低俗/血腥暴力/政治敏感/广告引流/二维码/疑似未成年人擦边） → 违规|具体原因"
                "真人照片/普通图片但可能涉及轻微风险 → 驳回|具体原因"
                "安全内容（二次元/游戏截图/风景/宠物/美食等） → 通过"
                "如果看不清或不确定图片内容 → 驳回|无法确认图片内容"
            )
            image_inputs = await self.llm._prepare_llm_image_inputs(images, max_images=4)

        try:
            response = await provider.text_chat(
                system_prompt="你是QQ空间内容审核员，严格审核用户投稿内容，存在封号风险的内容一律判为违规。"
                              "普通内容不合规判为驳回，不要解释，不要多余的话。"
                              "真人照片或普通消息如果不涉及严重风险，判为驳回而非违规。",
                prompt=prompt,
                image_urls=image_inputs,
            )
            result_text = response.completion_text.strip()
            return self._parse_llm_result(result_text)
        except asyncio.TimeoutError as e:
            # 大模型超时 = 系统侧问题，绝不能当成用户违规。
            # 这正是"超时累计 3 次被永久拉黑"的 bug 根因，标记 error=True。
            logger.error(f"[PublishReview] LLM 审核超时: {e}（系统问题，不记违规）")
            return {"status": "rejected", "error": True, "reason": "审核超时，请稍后再试"}
        except Exception as e:
            # 其它异常同样是系统侧问题（网络、provider 报错等），不记违规。
            logger.error(f"[PublishReview] LLM 审核异常: {e}（系统问题，不记违规）")
            return {"status": "rejected", "error": True, "reason": "审核服务暂时不可用，请稍后再试"}

    def _parse_llm_result(self, text: str) -> dict[str, Any]:
        text = text.strip()
        text = LLMAction.strip_thinking(text)
        text = text.strip()

        if text.startswith("通过"):
            return {"status": "approved", "reason": ""}

        if text.startswith("违规"):
            parts = text.split("|", 1)
            reason = parts[1].strip() if len(parts) > 1 else "内容涉及严重违规"
            return {"status": "violation", "reason": reason}

        if text.startswith("驳回") or text.startswith("不通过"):
            parts = text.split("|", 1)
            reason = parts[1].strip() if len(parts) > 1 else "内容不符合规范"
            return {"status": "rejected", "reason": reason}

        # 模糊匹配：检查前 20 个字符
        prefix = text[:20]
        if "违规" in prefix:
            return {"status": "violation", "reason": "内容涉及严重违规"}
        if "不通过" in prefix or "驳回" in prefix or "拒绝" in prefix:
            return {"status": "rejected", "reason": "内容审核不通过"}
        if "通过" in prefix:
            return {"status": "approved", "reason": ""}

        logger.warning(f"[PublishReview] LLM 返回结果无法解析: {text!r}，默认放行")
        return {"status": "approved", "reason": "结果无法解析，默认放行"}
