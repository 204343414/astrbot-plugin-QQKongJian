"""
astrbot_plugin_qqkongjian - QQ空间插件（多文件模块化版）

模块结构：
├── main.py              ← 插件入口 & AstrBot 路由（本文件）
├── config.py            ← 配置系统
├── model.py             ← 数据模型 (Post, Comment, QzoneContext, ApiResponse)
├── db.py                ← 数据库层 (PostDB)
├── parser.py            ← QQ空间响应解析
├── qzone_session.py     ← QQ空间登录会话
├── qzone_client.py      ← QQ空间HTTP客户端
├── qzone_api.py         ← QQ空间API封装
├── utils.py             ← 通用工具函数
├── llm_action.py        ← LLM动作 (评论生成/点赞判断)
├── service.py           ← 业务服务层
├── sender.py            ← 消息发送与渲染
├── scheduler.py         ← 定时任务 (AutoComment)
└── publish_review.py    ← 投稿审核 (LLM审 + 黑名单)
"""

from __future__ import annotations

# 修复 AstrBot 加载时找不到同级模块的问题
import sys
from pathlib import Path
_plugin_dir = Path(__file__).resolve().parent
if str(_plugin_dir) not in sys.path:
    sys.path.insert(0, str(_plugin_dir))
else:
    # 确保本插件目录在最前，避免被同名目录抢先
    sys.path.remove(str(_plugin_dir))
    sys.path.insert(0, str(_plugin_dir))

# ⚠️ 兄弟模块隔离：本插件用裸名 import（from utils import ...）导入同级模块。
# 这些名字（utils/config/db...）很通用，Python 的 sys.modules 是全局缓存，会引发：
#   1) 插件热重载：旧版本的 utils 残留在 sys.modules，新加的函数导入不到，
#      报 “cannot import name build_command_publish_text from utils”；
#   2) 跨插件撞名：别的插件也有同名 utils.py 时互相顶替。
# 解决：在导入兄弟模块之前，把这些同名模块从 sys.modules 里**无条件**清掉，
# 强制接下来的 import 从本插件目录（已置于 sys.path[0]）重新读取最新文件。
# 这些模块都是“定义函数/类”的无状态模块，重新导入是安全的；下面紧接着就会
# 按依赖顺序重新 import 它们。
_SIBLING_MODULE_NAMES = (
    "config", "model", "db", "parser", "qzone_session", "qzone_client",
    "qzone_api", "utils", "llm_action", "service", "sender", "scheduler",
    "publish_review",
)
for _name in _SIBLING_MODULE_NAMES:
    sys.modules.pop(_name, None)

import asyncio
import random
import re
import shutil
import time
from datetime import datetime
from typing import Any

from aiocqhttp import CQHttp
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.star.star_tools import StarTools

# ---- 内部模块 ----
from config import PluginConfig
from model import Post
from db import PostDB
from parser import QzoneParser
from qzone_session import QzoneSession
from qzone_api import QzoneAPI
from utils import get_ats, get_nickname, resolve_target_id, parse_range, get_image_urls, get_reply_message_str, build_command_publish_text, convert_ats_to_qzone, build_at_map
from llm_action import LLMAction
from service import PostService
from sender import Sender
from scheduler import AutoComment
from publish_review import PublishReview


# ============================================================
# 空间可访问性判定（参考 Zhalslar/astrbot_plugin_qzone 的 _map_feed_error）
# ============================================================

# 错误关键词：QQ 空间 API 返回"对方没对 bot 开放空间 / 私密 / 无权限"等语义时会出现
_INACCESSIBLE_SPACE_KEYWORDS: tuple[str, ...] = (
    "无权限",
    "权限不足",
    "权限",
    "私密",
    "不可见",
    "拒绝访问",
    "受限",
    "forbidden",
    "access denied",
    "未开通",
    "未开放",
    "未对您",
    "对您",
    "对方没有",
    "对方空间",
    "开放空间",
    "设置权限",
    "设置了权限",
    "仅好友",
    "查无此人",
    "查无此",
    "账号不存在",
    "账号已注销",
    "用户不存在",
    "QQ号不存在",
    "该用户",
    "Empty",
    "not found",
    "not open",
    "closed",
    "private",
)

# 区分：登录/会话失效不算"对方不可访问"，避免错误写入 ignore
_LOGIN_ERROR_KEYWORDS: tuple[str, ...] = (
    "登录",
    "失效",
    "skey",
    "g_tk",
    "cookie",
    "expired",
    "重新登录",
    "未登录",
)


def _looks_like_inaccessible_space(err: BaseException | str, *, code: Any = None) -> bool:
    """判断一条 API 错误是否表示「对方没对 bot 开放空间 / 不可访问」"""
    # 显式 code：QQ 空间 API 权限不足通常返回 403 / -403 / -5
    if code in (403, -403, -5, -6, 4):
        return True
    msg = str(err or "").lower()
    if not msg:
        return False
    # 登录/会话失效优先返回 False，避免把会话问题当成"对方不可访问"
    if any(kw.lower() in msg for kw in _LOGIN_ERROR_KEYWORDS):
        return False
    return any(kw.lower() in msg for kw in _INACCESSIBLE_SPACE_KEYWORDS)


def _looks_like_login_error(err: BaseException | str) -> bool:
    msg = str(err or "").lower()
    if not msg:
        return False
    return any(kw.lower() in msg for kw in _LOGIN_ERROR_KEYWORDS)


# ============================================================
# AstrBot插件入口：QzonePlugin
# ============================================================

class QzonePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 配置
        self.cfg = PluginConfig(config, context)
        # 会话
        self.session = QzoneSession(self.cfg)
        # QQ空间
        self.qzone = QzoneAPI(self.session, self.cfg)
        # 数据库
        self.db = PostDB(self.cfg)
        # LLM模块
        self.llm = LLMAction(self.cfg, None)
        # 消息发送器
        self.sender = Sender(self.cfg)
        # 操作服务
        self.service = PostService(self.qzone, self.session, self.db, self.llm)
        # 自动评论模块
        self.auto_comment: AutoComment | None = None
        # 投稿审核模块
        self.publish_review: PublishReview | None = None
        # 已互动的说说tid缓存
        self._interacted_tids: set[str] = set()
        # 概率触发锁
        self._prob_lock = asyncio.Lock()
        self._ignore_cleanup_done = False
        self._ignore_cleanup_last_ts: float = 0.0
        self._prob_last_interact_ts: float = 0.0
        self._prob_daily_key: str = ""
        self._prob_daily_count: int = 0
        self._prob_min_interval_sec: int = 30 * 60
        self._prob_daily_limit: int = 5
        # 好友列表短缓存：投稿/转发前用于确认“投稿人是 bot 好友”。
        self._friend_ids_cache: set[str] = set()
        self._friend_ids_cache_ts: float = 0.0
        self._friend_ids_cache_ttl: int = 5 * 60

    async def initialize(self):
        """插件加载时触发"""
        await self.db.initialize()
        if not self.auto_comment and self.cfg.trigger.comment_cron:
            self.auto_comment = AutoComment(self.cfg, self.service, self.sender)
        # 初始化投稿审核模块
        self.publish_review = PublishReview(self.cfg, self.db, self.llm)
        await self.publish_review.initialize()
        # 启动时立即清理一次 ignore_users（把已经成为好友的 QQ 从列表中移除）。
        # 注意：self.cfg.client 此时可能还没注入（要等第一个事件），
        # 所以 _cleanup_ignore_users_by_friend_list 会自动跳过；会在 prob_read_feed / friend 事件 / 下次冷却时自动补救。
        await self._cleanup_ignore_users_by_friend_list(force=True)

    async def terminate(self):
        """插件卸载时"""
        if self.qzone:
            await self.qzone.close()
        if self.auto_comment:
            await self.auto_comment.terminate()
        if self.publish_review:
            await self.publish_review.close()
        if self.cfg.cache_dir.exists():
            try:
                shutil.rmtree(self.cfg.cache_dir)
            except Exception as e:
                logger.error(f"清理缓存失败: {e}")

    async def _cleanup_ignore_users_by_friend_list_once(self):
        if self._ignore_cleanup_done or not self.cfg.client:
            return
        self._ignore_cleanup_done = True
        try:
            friend_list = await self.cfg.client.get_friend_list()
            friend_ids = {str(f.get("user_id")) for f in friend_list}
            removable = [uid for uid in list(self.cfg.source.ignore_users) if str(uid) in friend_ids]
            if removable:
                self.cfg.remove_ignore_users(removable)
                logger.info(f"已从忽略列表移除已成为好友的用户：{removable}")
        except Exception as e:
            logger.debug(f"清理忽略列表失败：{e}")

    async def _cleanup_ignore_users_by_friend_list(self, *, force: bool = False):
        """带冷却的清理：成为好友的用户从 ignore_users 移除。

        - force=True：跳过冷却（用于初始化时立即执行一次）。
        - 其它情况：默认 30 分钟冷却，避免反复 get_friend_list 浪费接口配额。
        """
        cleanup_on_friend_change = bool(
            getattr(self.cfg.trigger, "ignore_user_cleanup_on_friend_change", True)
        )
        if not cleanup_on_friend_change:
            return
        if not self.cfg.client:
            return
        now = time.time()
        if not force and self._ignore_cleanup_last_ts and now - self._ignore_cleanup_last_ts < 1800:
            return
        self._ignore_cleanup_last_ts = now
        try:
            friend_list = await self.cfg.client.get_friend_list()
            friend_ids = {str(f.get("user_id")) for f in friend_list}
            removable = [uid for uid in list(self.cfg.source.ignore_users) if str(uid) in friend_ids]
            if removable:
                self.cfg.remove_ignore_users(removable)
                logger.info(f"已从忽略列表移除已成为好友的用户：{removable}")
        except Exception as e:
            logger.debug(f"清理忽略列表失败：{e}")

    def _record_unreadable_user(self, target_id: str, *, reason: str, source: str) -> bool:
        """把探测失败（空间不可访问）的用户加入忽略列表。

        Returns: 是否成功写入（已存在则不重复写入）。
        """
        if not target_id:
            return False
        uid = str(target_id).strip()
        if not uid.isdigit():
            return False
        if self.cfg.source.is_ignore_user(uid):
            return False
        self.cfg.append_ignore_users(uid)
        # 取一段简短的原因摘要
        summary = (reason or "").strip().replace("\n", " ")
        if len(summary) > 80:
            summary = summary[:80] + "…"
        logger.info(
            f"[{source}] 探测 QQ {uid} 空间失败/不可访问，已加入忽略列表。reason={summary}"
        )
        return True

    async def _on_friend_added(self, user_id: str) -> None:
        """bot 与某人成为好友时调用：清理 ignore_users（让下次探测重新判定）。"""
        cleanup_on_friend_change = bool(
            getattr(self.cfg.trigger, "ignore_user_cleanup_on_friend_change", True)
        )
        if not cleanup_on_friend_change:
            return
        uid = str(user_id or "").strip()
        if not uid.isdigit():
            return
        if self.cfg.source.is_ignore_user(uid):
            self.cfg.remove_ignore_users(uid)
            logger.info(f"已成为好友 QQ {uid}，已从忽略列表移除（下次探测会重新判定）")

    def _today_start_ts(self) -> int:
        now = datetime.now(self.cfg.timezone)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp())

    async def _can_auto_comment(self, post: Post) -> bool:
        if not post.tid:
            return False
        if await self.db.has_interaction(action="space_comment", tid=post.tid):
            return False
        daily_limit = int(self.cfg.trigger.auto_comment_per_user_daily_limit or 3)
        cooldown_minutes = int(self.cfg.trigger.auto_comment_per_user_cooldown_minutes or 180)
        today_count = await self.db.count_interactions_since(
            action="space_comment", target_uin=post.uin,
            since_ts=self._today_start_ts(), source_prefix="auto",
        )
        if daily_limit >= 0 and today_count >= daily_limit:
            return False
        last_ts = await self.db.last_interaction_ts(
            action="space_comment", target_uin=post.uin, source_prefix="auto",
        )
        if last_ts and time.time() - last_ts < cooldown_minutes * 60:
            return False
        try:
            bot_uin = await self.session.get_uin()
            if any(c.uin == bot_uin for c in post.comments):
                await self.db.log_interaction(
                    action="space_comment", source="auto_found_existing",
                    tid=post.tid, target_uin=post.uin,
                )
                return False
        except Exception:
            pass
        return True

    async def _can_group_show(self, post: Post, group_id: str) -> bool:
        if not post.tid or not group_id:
            return False
        if await self.db.has_interaction(action="group_show", tid=post.tid, group_id=group_id):
            return False
        daily_limit = int(self.cfg.trigger.group_show_per_user_daily_limit or 3)
        today_count = await self.db.count_interactions_since(
            action="group_show", target_uin=post.uin, group_id=group_id,
            since_ts=self._today_start_ts(),
        )
        return daily_limit < 0 or today_count < daily_limit

    async def _auto_comment_if_allowed(self, post: Post, *, source: str) -> tuple[bool, bool]:
        commented = False
        liked = False
        if await self._can_auto_comment(post):
            try:
                await self.service.comment_posts(post)
                commented = True
            except Exception as e:
                logger.error(f"自动评论失败，已改为只展示不打扰：{e}")
                return False, False
            if post.tid:
                self._interacted_tids.add(post.tid)
                await self.db.log_interaction(
                    action="space_comment", source=source,
                    tid=post.tid, target_uin=post.uin,
                )
            if self.cfg.trigger.like_when_comment:
                try:
                    if await self.llm.should_like(post):
                        await self.service.like_posts(post)
                        liked = True
                except Exception as e:
                    logger.error(f"自动点赞判断失败：{e}")
        return commented, liked

    async def _find_latest_post_for_group_show(self, *, target_id: str, force: bool = False) -> tuple[Post | None, str]:
        diagnostics: list[str] = []
        try:
            recent_posts = await self.service.query_feeds(
                pos=0, num=30, with_detail=False,
                no_self=not force, no_commented=False,
            )
            recent_uins = []
            for p in recent_posts:
                recent_uins.append(str(p.uin))
                if str(p.uin) == str(target_id):
                    diagnostics.append(f"好友动态流命中 target_id={target_id}, tid={p.tid}")
                    return p, "\n".join(diagnostics)
            diagnostics.append("好友动态流未命中；最近解析到的uin=" + ",".join(recent_uins[:12]))
        except Exception as e:
            err_msg = str(e)
            diagnostics.append(f"好友动态流读取失败：{err_msg}")
            # 好友动态流是全局接口，失败原因不一定是 target_id 本身的问题（例如会话失效/风控）。
            # 仅在错误信息明确指向"无权限/私密/不可见"时, 才把 target_id 加入 ignore。
            if not _looks_like_login_error(err_msg) and _looks_like_inaccessible_space(err_msg):
                self._record_unreadable_user(
                    target_id, reason=err_msg, source="probe_feed_stream"
                )

        try:
            posts = await self.service.query_feeds(
                target_id=target_id, pos=0, num=1,
                with_detail=True, no_self=not force, no_commented=False,
            )
            if posts:
                diagnostics.append(f"个人主页详情接口命中 tid={posts[0].tid}")
                return posts[0], "\n".join(diagnostics)
        except Exception as e:
            err_msg = str(e)
            diagnostics.append(f"个人主页详情读取错误：{err_msg}")
            if not _looks_like_login_error(err_msg) and _looks_like_inaccessible_space(err_msg):
                self._record_unreadable_user(
                    target_id, reason=err_msg, source="probe_personal_detail"
                )

        try:
            posts = await self.service.query_feeds(
                target_id=target_id, pos=0, num=1,
                with_detail=False, no_self=not force, no_commented=False,
            )
            if posts:
                diagnostics.append(f"个人主页列表接口命中 tid={posts[0].tid}")
                return posts[0], "\n".join(diagnostics)
        except Exception as e:
            err_msg = str(e)
            diagnostics.append(f"个人主页列表读取错误：{err_msg}")
            if not _looks_like_login_error(err_msg) and _looks_like_inaccessible_space(err_msg):
                self._record_unreadable_user(
                    target_id, reason=err_msg, source="probe_personal_list"
                )

        return None, "\n".join(diagnostics)

    async def _show_latest_post_in_group(self, event: AiocqhttpMessageEvent, *,
                                          target_id: str, group_id: str,
                                          source: str, force: bool = False) -> bool:
        post, diagnostics = await self._find_latest_post_for_group_show(target_id=target_id, force=force)
        if not post:
            if force:
                await event.send(event.plain_result(
                    "测试触发读取失败：没有拿到说说。\n"
                    f"target_id={target_id}, group_id={group_id}, bot_self_id={event.get_self_id()}\n"
                    + diagnostics
                ))
            return False
        if not post.tid:
            if force:
                await event.send(event.plain_result(
                    f"测试触发失败：读取到说说但 tid 为空。target_id={target_id}, post_uin={post.uin}, text={post.text[:40]}\n{diagnostics}"
                ))
            return False
        if not force and not await self._can_group_show(post, str(group_id)):
            logger.info(f"群展示跳过：group={group_id}, uin={post.uin}, tid={post.tid} 已展示或达到每日上限")
            return False

        try:
            safe_to_forward, unsafe_reason = await self.llm.review_post_for_forward(post)
            if not safe_to_forward:
                logger.warning(f"群展示跳过：tid={post.tid}, uin={post.uin}, reason={unsafe_reason}")
                if force:
                    await event.send(event.plain_result(f"测试触发展示已跳过：该说说不适合搬运。原因：{unsafe_reason}"))
                return False

            content_for_risk = "\n".join(x for x in [post.text, post.rt_con] if x)
            is_critical = LLMAction.is_critical_risk_content(content_for_risk)
            was_commented = await self.db.has_interaction(action="space_comment", tid=post.tid)
            commented, liked = await self._auto_comment_if_allowed(post, source=source)
            if is_critical:
                msg = "看到这条有点心疼，大家温柔一点"
            elif commented:
                msg = "触发读说说：已评论并点赞" if liked else "触发读说说：已评论"
            elif was_commented:
                msg = "触发读说说：已评论并点赞" if self.cfg.trigger.like_when_comment else "触发读说说：已评论"
            else:
                msg = "触发读说说：搬来给大家看看"
            await self.sender.send_post(event, post, message=msg, send_admin=False if force else self.cfg.trigger.send_admin)
            if not force:
                await self.db.log_interaction(
                    action="group_show", source=source,
                    tid=post.tid, target_uin=post.uin,
                    group_id=str(group_id), actor_uin=event.get_sender_id(),
                )
            return True
        except Exception as e:
            logger.error(f"群展示说说失败：{e}")
            if force:
                await event.send(event.plain_result(f"测试触发展示失败：{e}\n{diagnostics}"))
            return False

    # ---- 概率触发 ----

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def prob_read_feed(self, event: AiocqhttpMessageEvent):
        if not self.cfg.client:
            self.cfg.client = event.bot
            logger.debug("QQ空间所需的 CQHttp 客户端已初始化")
        group_id = event.get_group_id()
        if not group_id:
            return
        if self.cfg.source.is_ignore_group(str(group_id)):
            return
        await self._cleanup_ignore_users_by_friend_list_once()
        # 顺带按冷却窗口再做一次（不强制），让新增的好友关系能在 30 分钟内被自动清理出 ignore 列表。
        await self._cleanup_ignore_users_by_friend_list()
        sender_id = event.get_sender_id()
        if self.cfg.source.is_ignore_user(sender_id):
            return
        if not bool(self.cfg.trigger.auto_read_enabled if self.cfg.trigger.auto_read_enabled is not None else True):
            return
        if self.cfg.trigger.read_prob <= 0:
            return
        if random.random() >= self.cfg.trigger.read_prob:
            return
        probe_cooldown = int(self.cfg.trigger.auto_probe_cooldown_minutes or 5)
        if probe_cooldown > 0:
            last_probe = await self.db.last_interaction_ts(
                action="auto_probe", target_uin=sender_id,
                source_prefix="auto_group_prob",
            )
            if last_probe and time.time() - last_probe < probe_cooldown * 60:
                return
        if self._prob_lock.locked():
            return
        async with self._prob_lock:
            await self.db.log_interaction(
                action="auto_probe", source="auto_group_prob",
                target_uin=sender_id, group_id=str(group_id),
                actor_uin=sender_id,
            )
            await self._show_latest_post_in_group(
                event, target_id=sender_id, group_id=str(group_id),
                source="auto_group_prob", force=False,
            )

    # ---- 好友申请 / 群邀请监听（仿 auto_approve_all）----

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_request_event(self, event: AstrMessageEvent):
        """
        监听 QQ 后端的 "request" 事件：
          - request_type == "friend"            → 自动同意好友申请 + 清理 ignore_users
          - request_type == "group" + invite    → 自动同意群邀请（可选，默认关闭，避免误进群）
        设计思路：
          1) set_friend_add_request / set_group_add_request 是幂等的，
             即使和别的自动同意插件共存也不会重复处理（多次 approve=true 仍 ok）。
          2) 成为好友后, 如果对方之前在 ignore_users 里(探测过发现空间不可访问),
             就把他移出去, 让下次概率触发/cron/LLM访问重新判定。
             若重新探测仍不可访问, 探测本身又会把他加回 ignore_users —— 自愈闭环。
          3) 群邀请默认关闭：QQ空间插件的主要职责是好友+说说, 不该替用户做加群决策。
             管理员可在配置文件里手动启用 auto_approve_group_invite。
        """
        try:
            raw_message = getattr(event.message_obj, "raw_message", None)
        except Exception:
            raw_message = None
        if not isinstance(raw_message, dict) or raw_message.get("post_type") != "request":
            return
        if not isinstance(event, AiocqhttpMessageEvent):
            return

        client = event.bot
        flag = raw_message.get("flag")
        user_id = str(raw_message.get("user_id") or "")
        request_type = raw_message.get("request_type")
        sub_type = raw_message.get("sub_type")

        # 好友申请：自动同意 + 清理 ignore_users
        if request_type == "friend" and flag:
            auto_approve = bool(
                getattr(self.cfg.trigger, "auto_approve_friend_request", True)
            )
            if not auto_approve:
                return
            try:
                await client.set_friend_add_request(flag=flag, approve=True)
                logger.info(f"已自动同意好友申请 from {user_id}")
            except Exception as e:
                logger.error(f"自动同意好友申请失败：{e}")
                return
            # 同意成功后：清理 ignore_users（让下次探测重新判定）
            await self._on_friend_added(user_id)
            return

        # 群邀请：默认不处理（QQ空间插件不该替用户加群）；保留扩展位。
        if request_type == "group" and sub_type == "invite" and flag:
            # 如需开启, 在 TriggerConfig 加字段 auto_approve_group_invite。
            # 留空函数, 不做默认行为, 避免误进群。
            return

    # ---- 辅助方法 ----

    async def _get_posts(self, event: AiocqhttpMessageEvent, *,
                          target_id: str | None = None,
                          with_detail: bool = False,
                          no_commented=False, no_self=False) -> list[Post]:
        pos, num = parse_range(event)
        at_ids = get_ats(event)
        if not target_id:
            target_id = at_ids[0] if at_ids else None
        try:
            logger.debug(f"正在查询说说： {target_id, pos, num, with_detail, no_commented, no_self}")
            posts = await self.service.query_feeds(
                target_id=target_id, pos=pos, num=num,
                with_detail=with_detail, no_commented=no_commented, no_self=no_self,
            )
            if not posts:
                await event.send(event.plain_result("查询结果为空"))
                event.stop_event()
                return posts
            # 查询成功（拿到有效 posts）：把目标从 ignore 列表移除（说明现在能看到空间了）。
            # 之前这里是"查询前就 remove"，会让 ignore 列表失去意义——
            # 哪怕对方根本不可访问, 也会被静默移出。
            if target_id:
                self.cfg.remove_ignore_users(target_id)
            return posts
        except Exception as e:
            err_msg = str(e)
            # 查询失败：若是空间不可访问（无权限/私密/不可见等）→ 写入 ignore；
            # 若是登录失效/网络抖动 → 不要写入 ignore，留给下次重试。
            if target_id and not _looks_like_login_error(err_msg) and _looks_like_inaccessible_space(err_msg):
                self._record_unreadable_user(
                    target_id, reason=err_msg, source="manual_view"
                )
            await event.send(event.plain_result(err_msg))
            logger.error(err_msg)
            event.stop_event()
            return []

    # ---- 命令路由 ----

    @filter.command("qq空间_看说说", alias={"qq空间_查看说说"})
    async def view_feed(self, event: AiocqhttpMessageEvent, arg: str | None = None):
        posts = await self._get_posts(event, with_detail=True)
        for post in posts:
            safe_to_forward, unsafe_reason = await self.llm.review_post_for_forward(post)
            if not safe_to_forward:
                await event.send(event.plain_result(f"这条说说不适合搬运展示，已跳过。原因：{unsafe_reason}"))
                continue
            await self.sender.send_post(event, post)

    @filter.command("qq空间_评说说", alias={"qq空间_评论说说", "qq空间_读说说"})
    async def comment_feed(self, event: AiocqhttpMessageEvent):
        ats = get_ats(event)
        parts = event.message_str.strip().split()
        has_args = bool(ats) or len(parts) > 1
        if has_args:
            posts = await self._get_posts(event, no_commented=True, no_self=True)
            for post in posts:
                try:
                    safe_to_forward, unsafe_reason = await self.llm.review_post_for_forward(post)
                    if not safe_to_forward:
                        await event.send(event.plain_result(f"这条说说不适合评论或搬运展示，已跳过。原因：{unsafe_reason}"))
                        continue

                    await self.service.comment_posts(post)
                    msg = "已评论"
                    if self.cfg.trigger.like_when_comment:
                        if await self.llm.should_like(post):
                            await self.service.like_posts(post)
                            msg += "并点赞"
                        else:
                            msg += "（LLM判断不宜点赞）"
                    await self.sender.send_post(event, post, message=msg)
                except Exception as e:
                    await event.send(event.plain_result(str(e)))
                    logger.error(e)
        else:
            await self._random_friend_interact(event)

    async def _random_friend_interact(self, event: AiocqhttpMessageEvent):
        if not self.cfg.client:
            await event.send(event.plain_result("客户端未初始化，请先发送任意消息"))
            return
        try:
            friend_list = await self.cfg.client.get_friend_list()
        except Exception as e:
            await event.send(event.plain_result(f"获取好友列表失败：{e}"))
            return
        friend_ids = [str(f["user_id"]) for f in friend_list]
        self_id = event.get_self_id()
        friend_ids = [fid for fid in friend_ids if fid != self_id and not self.cfg.source.is_ignore_user(fid)]
        if not friend_ids:
            await event.send(event.plain_result("没有可互动的好友"))
            return
        random.shuffle(friend_ids)
        await event.send(event.plain_result("正在随机寻找好友的新说说..."))
        for fid in friend_ids[:50]:
            try:
                posts = await self.service.query_feeds(
                    target_id=fid, pos=0, num=1,
                    no_self=True, no_commented=True,
                )
            except Exception as e:
                logger.debug(f"跳过好友 {fid}：{e}")
                err_msg = str(e)
                if not _looks_like_login_error(err_msg) and _looks_like_inaccessible_space(err_msg):
                    self._record_unreadable_user(
                        fid, reason=err_msg, source="random_friend_interact"
                    )
                await asyncio.sleep(1)
                continue
            if not posts:
                await asyncio.sleep(1)
                continue
            post = posts[0]
            if post.tid and post.tid in self._interacted_tids:
                logger.debug(f"跳过已互动的说说：{post.tid}")
                await asyncio.sleep(1)
                continue
            try:
                safe_to_forward, unsafe_reason = await self.llm.review_post_for_forward(post)
                if not safe_to_forward:
                    logger.warning(f"随机互动跳过不适合搬运/互动的说说：tid={post.tid}, uin={post.uin}, reason={unsafe_reason}")
                    await asyncio.sleep(1)
                    continue

                await self.service.comment_posts(post)
                msg = "已评论"
                if self.cfg.trigger.like_when_comment:
                    if await self.llm.should_like(post):
                        await self.service.like_posts(post)
                        msg += "并点赞"
                    else:
                        msg += "（LLM判断不宜点赞）"
                if post.tid:
                    self._interacted_tids.add(post.tid)
                await self.sender.send_post(event, post, message=msg)
                return
            except Exception as e:
                logger.error(f"互动好友 {fid} 失败：{e}")
                await asyncio.sleep(1)
                continue
        await event.send(event.plain_result("遍历好友后未找到可互动的新说说，可能都已评论过"))

    async def _get_client_from_platforms(self):
        """按 HappyBirthday 插件的方式兜底获取 aiocqhttp client。"""
        if self.cfg.client:
            return self.cfg.client
        try:
            platforms = self.context.platform_manager.get_insts()
            for platform in platforms:
                if hasattr(platform, "get_client"):
                    client = platform.get_client()
                    if client:
                        self.cfg.client = client
                        return client
        except Exception as e:
            logger.debug(f"从平台实例获取 client 失败：{e}")
        return None

    async def _ensure_client(self, event: AiocqhttpMessageEvent):
        if not self.cfg.client:
            self.cfg.client = event.bot
        return self.cfg.client or await self._get_client_from_platforms()

    async def _is_bot_friend(self, user_id: str, *, force_refresh: bool = False) -> bool:
        """判断 user_id 是否是 bot 好友；无法确认时保守返回 False。"""
        client = await self._get_client_from_platforms()
        if not client:
            logger.warning("无法校验好友关系：缺少 CQHttp client")
            return False
        now = time.time()
        if force_refresh or not self._friend_ids_cache or now - self._friend_ids_cache_ts > self._friend_ids_cache_ttl:
            try:
                friend_list = await client.get_friend_list()
                self._friend_ids_cache = {str(f.get("user_id")) for f in friend_list if f.get("user_id") is not None}
                self._friend_ids_cache_ts = now
            except Exception as e:
                logger.error(f"获取好友列表失败，拒绝本次投稿以保证安全：{e}")
                return False
        return str(user_id) in self._friend_ids_cache

    def _build_forward_review_text(self, source_post: Post) -> str:
        """构造审核文本：审核原帖内容，而不是只审核转发时的来源标注。"""
        parts: list[str] = []
        if (source_post.text or "").strip():
            parts.append((source_post.text or "").strip())
        if (source_post.rt_con or "").strip():
            parts.append(f"[原帖本身包含转发内容]\n{(source_post.rt_con or '').strip()}")
        if source_post.videos:
            parts.append(f"[视频说说，共{len(source_post.videos)}个视频]")
        return "\n\n".join(parts).strip()

    async def _load_latest_user_space_post(self, user_id: str) -> Post:
        """读取用户自己空间最新一条说说；优先读详情，失败则降级列表结果。"""
        try:
            posts = await self.service.query_feeds(
                target_id=str(user_id),
                pos=0,
                num=1,
                with_detail=True,
            )
        except Exception as detail_err:
            logger.debug(f"读取用户空间说说详情失败，降级使用列表结果 user_id={user_id}: {detail_err}")
            posts = await self.service.query_feeds(
                target_id=str(user_id),
                pos=0,
                num=1,
                with_detail=False,
            )
        if not posts:
            raise RuntimeError("没有读到你空间里的说说。请先在自己的 QQ 空间发一条说说，再对我说“我要投稿”。")
        source_post = posts[0]
        if not (source_post.text or "").strip() and not source_post.images and not source_post.videos and not (source_post.rt_con or "").strip():
            raise RuntimeError("你空间最新一条说说没有可投稿的文字、图片、视频或转发内容，请先发一条新的说说后再投稿。")
        return source_post

    async def _submit_latest_own_qzone_post(self, event: AiocqhttpMessageEvent) -> tuple[str, Post | None]:
        """投稿主流程：好友用户自己的最新空间说说 → 审核 → bot 原生转发。"""
        sender_id = str(event.get_sender_id())
        sender_name = event.get_sender_name() or sender_id
        await self._ensure_client(event)

        is_admin = sender_id in self.cfg.admins_id
        if not bool(self.cfg.trigger.publish_everyone_enabled if self.cfg.trigger.publish_everyone_enabled is not None else True) and not is_admin:
            return "当前只允许管理员投稿。", None

        if not await self._is_bot_friend(sender_id):
            return "只有 bot 好友才能投稿。请先加 bot 为好友，并确保你的 QQ 空间对 bot 可见。", None

        daily_limit = int(self.cfg.trigger.publish_per_user_daily_limit or 1)
        today_count = await self.db.count_interactions_since(
            action="submit_forward", actor_uin=sender_id,
            since_ts=self._today_start_ts(),
        )
        if daily_limit >= 0 and today_count >= daily_limit:
            return f"你今天已经成功投稿 {today_count} 条啦，明天再来～", None

        if self.publish_review is None:
            return "投稿审核模块未初始化，请稍后再试。", None
        if self.publish_review.is_banned(sender_id):
            return "你的投稿权限已被限制，无法继续投稿。", None

        source_post = await self._load_latest_user_space_post(sender_id)
        source_tid = str(source_post.tid or "")
        if source_tid and await self.db.has_interaction(action="submit_forward_source", tid=source_tid):
            return "你空间这条最新说说已经投稿过啦；如果要再投稿，请先在自己的空间发一条新的说说。", None

        review_text = self._build_forward_review_text(source_post)
        review = await self.publish_review.submit(
            user_id=sender_id,
            nickname=source_post.name or sender_name,
            text=review_text,
            images=source_post.images or [],
        )

        if review.status == review.BANNED:
            return "你的投稿权限已被限制，无法继续投稿。", None
        if review.status == review.ERROR:
            return f"审核服务暂时不可用，没有记你违规，请稍后再试。原因：{review.reason}", None
        if review.status == review.VIOLATION:
            remaining = self.publish_review.BAN_THRESHOLD - review.strikes
            if remaining <= 0:
                return "投稿内容涉及严重违规，已被禁止转发，且你的投稿权限已被永久限制。", None
            return f"投稿内容涉及严重违规，已被禁止转发。这是你第 {review.strikes} 次违规，累计 {self.publish_review.BAN_THRESHOLD} 次将永久限制投稿权限。原因：{review.reason}", None
        if review.status == review.REJECTED:
            return f"投稿审核未通过，请修改你空间里的说说后重新投稿。原因：{review.reason}", None

        # 原生转发时，bot 自己写在上方的正文只放来源标注；原帖内容由 QQ 空间转发卡片承载。
        forward_text = PublishReview.build_attribution_text(sender_id, source_post.name or sender_name, "")
        forwarded = await self.service.forward_post(source_post=source_post, content=forward_text)
        await self.db.log_interaction(
            action="submit_forward", source="submit_latest_own_qzone",
            tid=forwarded.tid, target_uin=forwarded.uin,
            group_id=event.get_group_id(), actor_uin=sender_id,
            extra=f"source_tid={source_tid};source_uin={source_post.uin}",
        )
        if source_tid:
            await self.db.log_interaction(
                action="submit_forward_source", source="submit_latest_own_qzone",
                tid=source_tid, target_uin=source_post.uin,
                group_id=event.get_group_id(), actor_uin=sender_id,
                extra=f"forwarded_tid={forwarded.tid or ''}",
            )
        return "投稿审核通过，已转发到 bot 空间", forwarded

    @filter.command("qq空间_投稿", alias={"我要投稿", "投稿"})
    async def submit_latest_feed(self, event: AiocqhttpMessageEvent):
        """用户先在自己空间发说说，再让 bot 原生转发实现投稿。"""
        try:
            msg, forwarded = await self._submit_latest_own_qzone_post(event)
            if forwarded:
                await self.sender.send_post(event, forwarded, message=msg)
                event.stop_event()
            else:
                yield event.plain_result(msg)
        except Exception as e:
            logger.error(f"投稿转发失败：{e}")
            yield event.plain_result(f"投稿失败：{e}")

    @filter.command("qq空间_发说说")
    async def publish_feed(self, event: AiocqhttpMessageEvent):
        sender_id = event.get_sender_id()
        is_admin = str(sender_id) in self.cfg.admins_id
        if not is_admin:
            yield event.plain_result("现在投稿统一改为转发你自己空间里的最新说说：请先在自己的 QQ 空间写一篇说说，然后对我说“我要投稿”。")
            return
        if not bool(self.cfg.trigger.publish_everyone_enabled if self.cfg.trigger.publish_everyone_enabled is not None else True) and not is_admin:
            yield event.plain_result("当前只允许管理员发说说。")
            return

        daily_limit = int(self.cfg.trigger.publish_per_user_daily_limit or 1)
        today_count = await self.db.count_interactions_since(
            action="publish", actor_uin=sender_id,
            since_ts=self._today_start_ts(),
        )
        if daily_limit >= 0 and today_count >= daily_limit and not is_admin:
            yield event.plain_result(f"你今天已经让 bot 发过 {today_count} 条说说啦，明天再来～")
            return

        # 从消息链重建正文：把 @某人 转成 QQ空间可点击、会提醒对方的 @好友
        # （message_str 会丢掉 At 段，所以不能直接用它）。
        text = await build_command_publish_text(event, ["qq空间_发说说"])
        allow_images = bool(self.cfg.trigger.publish_with_image if self.cfg.trigger.publish_with_image is not None else True)
        images = await get_image_urls(event) if allow_images else []
        sender_name = event.get_sender_name() or sender_id

        # ---- 投稿审核流程（LLM审 + 黑名单） ----
        if not is_admin:
            if self.publish_review is None:
                yield event.plain_result("投稿审核模块未初始化，请稍后再试。")
                return
            if self.publish_review.is_banned(str(sender_id)):
                yield event.plain_result("你的投稿权限已被限制，无法继续投稿。")
                return

            review = await self.publish_review.submit(
                user_id=str(sender_id),
                nickname=sender_name,
                text=text,
                images=images,
            )

            if review.status == review.BANNED:
                yield event.plain_result("你的投稿权限已被限制，无法继续投稿。")
                return

            # 审核流程本身没跑成功（大模型超时/异常/不可用）→ 不算违规，提示稍后重试。
            if review.status == review.ERROR:
                yield event.plain_result(f"审核服务暂时不可用，没有记你违规，请稍后再试。原因：{review.reason}")
                return

            # 严重违规：记违规 +1，提示用户
            if review.status == review.VIOLATION:
                remaining = self.publish_review.BAN_THRESHOLD - review.strikes
                if remaining <= 0:
                    yield event.plain_result("投稿内容涉及严重违规，已被禁止发布，且你的投稿权限已被永久限制。")
                else:
                    yield event.plain_result(f"投稿内容涉及严重违规，已被禁止发布。这是你第 {review.strikes} 次违规，累计 {self.publish_review.BAN_THRESHOLD} 次将永久限制投稿权限。原因：{review.reason}")
                return

            # 普通驳回：不发布，不记违规
            if review.status == review.REJECTED:
                yield event.plain_result(f"投稿审核未通过，请修改后重新投稿。原因：{review.reason}")
                return

            publish_text = review.publish_text
            if not publish_text:
                if review.strikes >= self.publish_review.BAN_THRESHOLD:
                    yield event.plain_result("投稿未通过审核，且你已累计多次违规，以后将无法投稿。")
                else:
                    yield event.plain_result(f"投稿未通过审核，请修改后重新投稿。原因：{review.reason}")
                return

            try:
                post = await self.service.publish_post(text=publish_text, images=images or [])
                await self.db.log_interaction(
                    action="publish", source="publish_approved",
                    tid=post.tid, target_uin=post.uin,
                    group_id=event.get_group_id(), actor_uin=sender_id,
                )
                await self.sender.send_post(event, post, message="投稿审核通过，已发布")
                event.stop_event()
                return
            except Exception as e:
                yield event.plain_result(f"发布失败：{e}")
                logger.error(e)
                return

        # 管理员直接发布
        if bool(self.cfg.trigger.publish_with_attribution if self.cfg.trigger.publish_with_attribution is not None else True):
            text = PublishReview.build_attribution_text(str(sender_id), sender_name, text)
        try:
            post = await self.service.publish_post(text=text, images=images)
            await self.db.log_interaction(
                action="publish", source="manual_publish",
                tid=post.tid, target_uin=post.uin,
                group_id=event.get_group_id(), actor_uin=sender_id,
            )
            await self.sender.send_post(event, post, message="已发布")
            event.stop_event()
        except Exception as e:
            yield event.plain_result(str(e))
            logger.error(e)

    # ---- 管理员命令 ----

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("qq空间_测试触发")
    async def debug_trigger_read(self, event: AiocqhttpMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("测试触发只能在群聊里使用。")
            return
        at_ids = get_ats(event)
        target_id = at_ids[0] if at_ids else event.get_sender_id()
        ok = await self._show_latest_post_in_group(
            event, target_id=target_id, group_id=str(group_id),
            source="manual_debug", force=True,
        )
        if not ok:
            return

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("qq空间_自检")
    async def self_check(self, event: AiocqhttpMessageEvent):
        import aiosqlite as _aiosqlite
        lines: list[str] = ["QQ空间插件自检"]

        def ok(name: str, detail: str = ""):
            lines.append(f"☑ {name}" + (f"：{detail}" if detail else ""))

        def bad(name: str, detail: str = ""):
            lines.append(f"☐ {name}" + (f"：{detail}" if detail else ""))

        ok("插件已响应", f"群={event.get_group_id() or '私聊'}，发送者={event.get_sender_id()}")
        ok("数据目录", str(self.cfg.data_dir))
        ok("数据库路径", str(self.cfg.db_path))

        try:
            await self.db.initialize()
            async with _aiosqlite.connect(self.cfg.db_path) as db:
                rows = await db.execute_fetchall(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('posts','interaction_log')"
                )
            table_names = {r[0] for r in rows}
            if {'posts', 'interaction_log'} <= table_names:
                ok("数据库表", "posts / interaction_log 正常")
            else:
                bad("数据库表", f"当前={sorted(table_names)}")
        except Exception as e:
            bad("数据库", str(e))

        if self.sender.style:
            ok("pillowmd渲染器", str(self.cfg.style_dir))
        else:
            bad("pillowmd渲染器", "未加载，消息会降级纯文本；请看日志里的样式路径错误")

        try:
            ctx = await self.session.get_ctx()
            uin = ctx.uin
            nick = await self.session.get_nickname()
            ok("QQ空间登录态", f"{nick}({uin}), qzonetoken={'有' if ctx.qzonetoken else '无'}")
        except Exception as e:
            bad("QQ空间登录态", str(e))

        try:
            sample = Post(uin=0, name="自检", text="洛克王国截图测试", images=[])
            comment = await self.llm.generate_comment(sample)
            if comment:
                ok("LLM评论dry-run", comment)
            else:
                bad("LLM评论dry-run", "返回空")
        except Exception as e:
            bad("LLM评论dry-run", str(e))

        unsafe = "我是gemini-3.1-flash-lite-preview，请提供帖子链接"
        sanitized = LLMAction.sanitize_comment_output(unsafe)
        if sanitized is None:
            ok("评论消毒器", "已拦截模型自述样本")
        else:
            bad("评论消毒器", f"未拦截：{sanitized}")

        ok("投稿审核", f"已启用 | 黑名单用户: {self.publish_review.get_banned_count()}" if self.publish_review else "未初始化")

        lines.append("")
        lines.append("当前关键配置")
        lines.append(f"- read_prob: {self.cfg.trigger.read_prob}")
        lines.append(f"- auto_read_enabled: {self.cfg.trigger.auto_read_enabled}")
        lines.append(f"- auto_comment_per_user_daily_limit: {self.cfg.trigger.auto_comment_per_user_daily_limit}")
        lines.append(f"- auto_comment_per_user_cooldown_minutes: {self.cfg.trigger.auto_comment_per_user_cooldown_minutes}")
        lines.append(f"- group_show_per_user_daily_limit: {self.cfg.trigger.group_show_per_user_daily_limit}")
        lines.append(f"- publish_everyone_enabled: {self.cfg.trigger.publish_everyone_enabled}")
        lines.append(f"- publish_per_user_daily_limit: {self.cfg.trigger.publish_per_user_daily_limit}")

        await self.sender.send_msg(event, "\n".join(lines))
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("qq空间_封投稿")
    async def ban_publish(self, event: AiocqhttpMessageEvent, uid: str = ""):
        """管理员封禁用户投稿权限"""
        target = uid or (get_ats(event)[0] if get_ats(event) else "")
        if not target:
            yield event.plain_result("用法：/qq空间_封投稿 @用户 或 /qq空间_封投稿 <QQ号>")
            return
        await self.publish_review.add_strike(target, reason="管理员手动封禁")
        if self.publish_review.is_banned(target):
            yield event.plain_result(f"用户 {target} 已被封禁投稿权限。")
        else:
            yield event.plain_result(f"已给用户 {target} 记一次违规，累计 {self.publish_review.get_strikes(target)}/{self.publish_review.BAN_THRESHOLD} 次。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("qq空间_解封投稿")
    async def unban_publish(self, event: AiocqhttpMessageEvent, uid: str = ""):
        """管理员解封用户投稿权限。
        用法：/qq空间_解封投稿 @用户 或 /qq空间_解封投稿 <QQ号>
        或：/qq空间_解封投稿 all → 清空所有人的投稿违规记录（解封所有被封禁用户）
        """
        target = uid or (get_ats(event)[0] if get_ats(event) else "")
        if not target:
            yield event.plain_result("用法：/qq空间_解封投稿 @用户 或 /qq空间_解封投稿 <QQ号>，或 /qq空间_解封投稿 all（清空所有人）")
            return
        if target.lower() == "all":
            cleared = await self.publish_review.clear_all_strikes()
            yield event.plain_result(f"已清空所有 {cleared} 个用户的投稿违规记录，所有人已解封。")
            return
        await self.publish_review.clear_strikes(target)
        yield event.plain_result(f"用户 {target} 已解封投稿权限。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("qq空间_ban列表")
    async def ban_list(self, event: AiocqhttpMessageEvent):
        """查看投稿违规/Ban 列表"""
        if not self.publish_review:
            yield event.plain_result("投稿审核模块未初始化。")
            return
        records = self.publish_review.get_all_strike_records()
        if not records:
            yield event.plain_result("当前没有任何用户有投稿违规记录。")
            return
        lines = [f"📋 投稿违规/Ban 列表（共 {len(records)} 人）："]
        for r in records:
            icon = "🚫" if r["banned"] else "⚠️"
            status = "已封禁" if r["banned"] else f"{r['strikes']}/{self.publish_review.BAN_THRESHOLD}"
            reason_str = f" | 最近原因：{r['reason']}" if r.get("reason") else ""
            lines.append(f"{icon} QQ {r['user_id']}：违规 {status} 次{reason_str}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("qq空间_审核状态")
    async def check_ban_status(self, event: AiocqhttpMessageEvent, uid: str = ""):
        """查看用户投稿审核状态"""
        target = uid or (get_ats(event)[0] if get_ats(event) else "")
        if not target:
            yield event.plain_result("用法：/qq空间_审核状态 @用户 或 /qq空间_审核状态 <QQ号>")
            return
        strikes = self.publish_review.get_strikes(target)
        banned = self.publish_review.is_banned(target)
        if banned:
            status = f"🚫 已封禁（违规 {strikes}/{self.publish_review.BAN_THRESHOLD} 次）"
        elif strikes > 0:
            status = f"⚠️ 有违规记录（{strikes}/{self.publish_review.BAN_THRESHOLD} 次，再 {self.publish_review.BAN_THRESHOLD - strikes} 次将被封禁）"
        else:
            status = "✅ 正常（无违规记录）"
        yield event.plain_result(f"用户 {target} 投稿状态：{status}")

    # ---- 撤回投稿 ----

    def _fmt_publish_time(self, ts: int) -> str:
        if not ts:
            return "未知时间"
        try:
            return datetime.fromtimestamp(int(ts), self.cfg.timezone).strftime("%m-%d %H:%M")
        except Exception:
            return "未知时间"

    @staticmethod
    def _preview_text(text: str, limit: int = 40) -> str:
        t = (text or "").replace("\n", " ").strip()
        if not t:
            return "（无文字内容）"
        return t if len(t) <= limit else t[:limit] + "…"

    def _render_publish_list(self, records: list[dict]) -> str:
        """把投稿列表渲染成带序号的文本，供用户选择撤回哪条。"""
        lines = []
        for i, r in enumerate(records, 1):
            img = f"｜{r['image_count']}图" if r.get("image_count") else ""
            lines.append(
                f"{i}. [{self._fmt_publish_time(r['created_at'])}{img}] "
                f"{self._preview_text(r['text'])}\n   tid: {r['tid']}"
            )
        return "\n".join(lines)

    @filter.command("qq空间_我的投稿", alias={"qq空间_投稿列表"})
    async def my_publishes(self, event: AiocqhttpMessageEvent):
        """查看自己最近发布成功的投稿（含 tid，便于撤回）。"""
        sender_id = str(event.get_sender_id())
        records = await self.db.list_published_by_actor(sender_id, limit=10)
        if not records:
            yield event.plain_result("你还没有成功发布过投稿哦～")
            return
        yield event.plain_result(
            "你最近发布成功的投稿：\n" + self._render_publish_list(records) +
            "\n\n撤回用法：/qq空间_撤回投稿 <序号>  或  /qq空间_撤回投稿 <tid>"
        )

    @filter.command("qq空间_撤回投稿", alias={"qq空间_撤稿", "qq空间_删投稿"})
    async def withdraw_publish(self, event: AiocqhttpMessageEvent, arg: str = ""):
        """撤回自己投稿过的说说；管理员可撤回任意说说。

        用法：
          /qq空间_撤回投稿            → 列出自己的投稿
          /qq空间_撤回投稿 <序号>     → 撤回列表里的第 N 条
          /qq空间_撤回投稿 <tid>      → 按 tid 撤回
          管理员：/qq空间_撤回投稿 <tid> 可撤回任意说说
        """
        sender_id = str(event.get_sender_id())
        is_admin = sender_id in self.cfg.admins_id
        arg = (arg or "").strip()

        # 取该用户的投稿列表（用于序号选择和鉴权展示）
        records = await self.db.list_published_by_actor(sender_id, limit=10)

        # 无参数：展示列表引导
        if not arg:
            if not records and not is_admin:
                yield event.plain_result("你还没有成功发布过投稿，没有可撤回的内容～")
                return
            hint = ""
            if records:
                hint = "你最近的投稿：\n" + self._render_publish_list(records) + "\n\n"
            extra = "（管理员可直接用 tid 撤回任意说说）" if is_admin else ""
            yield event.plain_result(
                hint + f"请告诉我要撤回哪条：/qq空间_撤回投稿 <序号或tid> {extra}"
            )
            return

        # 解析目标 tid
        target_tid = ""
        if arg.isdigit() and 1 <= int(arg) <= len(records):
            # 纯数字且落在列表序号范围内 → 当作序号
            target_tid = records[int(arg) - 1]["tid"]
        else:
            # 否则当作 tid
            target_tid = arg

        # 鉴权：普通用户只能撤自己投稿过的；管理员任意
        if not is_admin:
            owned = any(r["tid"] == target_tid for r in records) or \
                await self.db.is_published_by_actor(sender_id, target_tid)
            if not owned:
                yield event.plain_result("只能撤回你自己投稿过的说说哦，这条不在你的投稿记录里～")
                return

        try:
            await self.service.withdraw_post(target_tid)
            await self.db.log_interaction(
                action="withdraw",
                source="manual_withdraw_admin" if is_admin else "manual_withdraw",
                tid=target_tid, actor_uin=sender_id,
                group_id=event.get_group_id(),
            )
            yield event.plain_result(f"已撤回说说（tid: {target_tid}）✅")
        except Exception as e:
            logger.error(f"撤回投稿失败：{e}")
            yield event.plain_result(f"撤回失败：{e}")

    # ---- LLM 工具 ----

    @filter.llm_tool()
    async def llm_submit_latest_qzone_post(self, event: AiocqhttpMessageEvent):
        """转发当前用户自己 QQ 空间最新说说，实现投稿。

        当用户表达“我要投稿”“投稿说说”“帮我转发我刚发的空间”“我想投稿到 bot 空间”等意图时，调用本工具。

        重要规则：
        - 不要向用户索要投稿正文，也不要把用户在聊天里发来的文字直接发布到 bot 空间。
        - 如果用户还没有在自己的 QQ 空间写说说，请直接告诉用户：
          “请先去你自己的 QQ 空间写一篇说说，然后再对我说‘我要投稿’，我会读取你空间最新一条说说并帮你转发投稿。”
        - 本工具会检查用户是否是 bot 好友；不是好友则拒绝。
        - 本工具会读取用户自己空间最新一条说说，按投稿审核三态逻辑审核，通过后由 bot 原生转发到 bot 空间。
        - bot 转发时，上方正文只写“【来自 @xxx 的投稿】”，原帖内容由 QQ 空间转发卡片显示。
        """
        try:
            msg, forwarded = await self._submit_latest_own_qzone_post(event)
            if forwarded:
                await self.sender.send_post(event, forwarded, message=msg)
                return "已读取用户自己空间最新说说，审核通过，并转发到 bot 空间。"
            return msg
        except Exception as e:
            logger.error(f"LLM投稿转发失败：{e}")
            return f"投稿失败：{e}"

    @filter.llm_tool()
    async def llm_publish_feed(self, event: AiocqhttpMessageEvent, text: str = "", get_image: bool = True):
        """已弃用：不要再用聊天正文直接让 bot 发说说/投稿。

        如果用户想“投稿说说/发到 bot 空间/帮我发一条动态”，请告诉用户：
        “请先去你自己的 QQ 空间写一篇说说，然后再对我说‘我要投稿’，我会使用投稿转发工具读取你空间最新一条说说，审核后由 bot 原生转发。”

        然后应调用 llm_submit_latest_qzone_post，而不是本工具。
        """
        return (
            "现在投稿统一改为转发用户自己空间里的最新说说。"
            "请先去你自己的 QQ 空间写一篇说说，然后对我说‘我要投稿’，"
            "我会读取你空间最新一条说说，审核后由 bot 原生转发到 bot 空间。"
        )

    @filter.llm_tool()
    async def llm_visit_friend_qzone(self, event: AiocqhttpMessageEvent, user_id: str | None = None):
        """只读访问某个用户的 QQ 空间最新说说。

        仅当用户要求“看看/访问/读取/查看某人的空间或最新说说”时使用本工具。本工具不会发布新说说、不会评论、不会点赞。
        如果用户要求“投稿说说/发布到 bot 空间/帮我发一条动态”，不要调用本工具，应先提示用户去自己的 QQ 空间写说说，然后调用 llm_submit_latest_qzone_post。

        Args:
            user_id(string): 要访问的 QQ 号。留空时默认访问当前发消息用户的空间；如果用户 @ 了别人或明确给出 QQ 号，应传入对应 QQ 号。
        """
        target_id = user_id or event.get_sender_id()
        try:
            posts = await self.service.query_feeds(target_id=target_id, pos=0, num=1, with_detail=True)
        except Exception as e:
            err_msg = str(e)
            logger.warning(f"LLM工具只读访问空间失败: {err_msg}")
            # 查询失败：若是空间不可访问 → 写入 ignore（下次概率触发不再探测）。
            if not _looks_like_login_error(err_msg) and _looks_like_inaccessible_space(err_msg):
                self._record_unreadable_user(
                    target_id, reason=err_msg, source="llm_visit"
                )
                return "访问失败：对方可能没有开放QQ空间权限，或者内容不可见。"
            return f"访问出错了：{err_msg}"
        if not posts:
            return "访问成功，但是空间是空的，最近没有发说说。"
        # 查询成功：把目标从 ignore 列表移除（说明现在能看到空间了）。
        self.cfg.remove_ignore_users(target_id)
        post = posts[0]
        safe_to_forward, unsafe_reason = await self.llm.review_post_for_forward(post)
        if not safe_to_forward:
            return f"访问成功，但这条说说不适合搬运展示，已跳过发送卡片。原因：{unsafe_reason}"
        await self.sender.send_post(event, post, message="只读查看空间：未评论，未点赞")
        text_preview = (post.text or post.rt_con or "（无文字内容）").replace("\n", " ")
        if len(text_preview) > 120:
            text_preview = text_preview[:120] + "…"
        return (
            "只读访问成功，已把最新说说卡片发到当前会话。\n"
            f"说说作者：{post.name}({post.uin})\n"
            f"内容摘要：{text_preview}\n"
            f"图片数：{len(post.images)}，视频数：{len(post.videos)}\n"
            "安全说明：本工具没有评论、没有点赞。请用自然语气告诉用户已查看。"
        )

    @filter.llm_tool()
    async def llm_list_my_publishes(self, event: AiocqhttpMessageEvent):
        """列出当前用户最近通过本 bot 成功发布到 QQ 空间的投稿。

        当用户想“撤回/删除我发过的说说”，但没有明确指出要撤哪一条时，应先调用本工具拿到投稿清单，
        再用自然语言把这些投稿（时间、内容摘要）念给用户听，请他确认要撤回哪一条；
        确认后再调用 llm_withdraw_feed 撤回。

        本工具是只读的，不会删除任何内容。
        """
        sender_id = str(event.get_sender_id())
        records = await self.db.list_published_by_actor(sender_id, limit=10)
        if not records:
            return "该用户最近没有通过本 bot 成功发布过投稿，没有可撤回的内容。"
        lines = []
        for i, r in enumerate(records, 1):
            img = f"，{r['image_count']}张图" if r.get("image_count") else ""
            lines.append(
                f"{i}. 发布时间 {self._fmt_publish_time(r['created_at'])}{img}；"
                f"内容：{self._preview_text(r['text'], 50)}；tid={r['tid']}"
            )
        return (
            "该用户最近的投稿如下（最新在前）：\n" + "\n".join(lines) +
            "\n\n请用自然语言把上面的投稿告诉用户，让他确认要撤回哪一条（可以报序号或内容），"
            "确认后调用 llm_withdraw_feed 并传入对应的 tid。不要擅自替用户决定撤哪条。"
        )

    @filter.llm_tool()
    async def llm_withdraw_feed(self, event: AiocqhttpMessageEvent, tid: str = ""):
        """撤回（删除）一条已经发布到 QQ 空间的投稿说说。

        使用前提：必须已经知道要撤回的具体说说 tid。如果用户没有指明撤哪条，
        应先调用 llm_list_my_publishes 拿到清单并让用户确认，拿到 tid 后再调用本工具。

        权限：普通用户只能撤回自己投稿过的说说；管理员可以撤回任意说说。
        本工具会真实删除 QQ 空间里的说说，请在用户明确确认后再调用。

        Args:
            tid(string): 要撤回的说说 tid（从 llm_list_my_publishes 的结果里获取）。必填。
        """
        sender_id = str(event.get_sender_id())
        is_admin = sender_id in self.cfg.admins_id
        tid = (tid or "").strip()

        if not tid:
            # 没给 tid：尝试帮用户列出投稿引导确认
            records = await self.db.list_published_by_actor(sender_id, limit=10)
            if not records:
                return "没有提供要撤回的说说 tid，且该用户也没有可撤回的投稿记录。"
            return (
                "缺少要撤回的说说 tid。请先用 llm_list_my_publishes 把用户的投稿列出来，"
                "让用户确认具体撤哪一条，再带上对应 tid 调用本工具。"
            )

        # 鉴权：普通用户只能撤自己投稿过的
        if not is_admin:
            owned = await self.db.is_published_by_actor(sender_id, tid)
            if not owned:
                return "这条说说不在该用户的投稿记录里，普通用户只能撤回自己投稿过的说说，已拒绝撤回。"

        try:
            await self.service.withdraw_post(tid)
            await self.db.log_interaction(
                action="withdraw",
                source="llm_withdraw_admin" if is_admin else "llm_withdraw",
                tid=tid, actor_uin=sender_id,
                group_id=event.get_group_id(),
            )
            return f"已成功撤回该说说（tid: {tid}）。请用自然语气告诉用户撤回成功。"
        except Exception as e:
            logger.error(f"LLM撤回投稿失败：{e}")
            return f"撤回失败：{e}"
