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
from utils import get_ats, get_nickname, resolve_target_id, parse_range, get_image_urls, get_reply_message_str
from llm_action import LLMAction
from service import PostService
from sender import Sender
from scheduler import AutoComment
from publish_review import PublishReview


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
        self._prob_last_interact_ts: float = 0.0
        self._prob_daily_key: str = ""
        self._prob_daily_count: int = 0
        self._prob_min_interval_sec: int = 30 * 60
        self._prob_daily_limit: int = 5

    async def initialize(self):
        """插件加载时触发"""
        await self.db.initialize()
        if not self.auto_comment and self.cfg.trigger.comment_cron:
            self.auto_comment = AutoComment(self.cfg, self.service, self.sender)
        # 初始化投稿审核模块
        self.publish_review = PublishReview(self.cfg, self.db, self.llm)
        await self.publish_review.initialize()

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
            diagnostics.append(f"好友动态流读取失败：{e}")

        try:
            posts = await self.service.query_feeds(
                target_id=target_id, pos=0, num=1,
                with_detail=True, no_self=not force, no_commented=False,
            )
            if posts:
                diagnostics.append(f"个人主页详情接口命中 tid={posts[0].tid}")
                return posts[0], "\n".join(diagnostics)
        except Exception as e:
            diagnostics.append(f"个人主页详情读取错误：{e}")

        try:
            posts = await self.service.query_feeds(
                target_id=target_id, pos=0, num=1,
                with_detail=False, no_self=not force, no_commented=False,
            )
            if posts:
                diagnostics.append(f"个人主页列表接口命中 tid={posts[0].tid}")
                return posts[0], "\n".join(diagnostics)
        except Exception as e:
            diagnostics.append(f"个人主页列表读取错误：{e}")
            if "不存在" in str(e):
                self.cfg.append_ignore_users(target_id)

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

    # ---- 辅助方法 ----

    async def _get_posts(self, event: AiocqhttpMessageEvent, *,
                          target_id: str | None = None,
                          with_detail: bool = False,
                          no_commented=False, no_self=False) -> list[Post]:
        pos, num = parse_range(event)
        at_ids = get_ats(event)
        if not target_id:
            target_id = at_ids[0] if at_ids else None
        if target_id:
            self.cfg.remove_ignore_users(target_id)
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
        except Exception as e:
            await event.send(event.plain_result(str(e)))
            logger.error(e)
            event.stop_event()
            return []

    # ---- 命令路由 ----

    @filter.command("qq空间_看说说", alias={"qq空间_查看说说"})
    async def view_feed(self, event: AiocqhttpMessageEvent, arg: str | None = None):
        posts = await self._get_posts(event, with_detail=True)
        for post in posts:
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
                if "Empty" in err_msg or "不存在" in err_msg or "权限" in err_msg:
                    self.cfg.append_ignore_users(fid)
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

    @filter.command("qq空间_发说说")
    async def publish_feed(self, event: AiocqhttpMessageEvent):
        sender_id = event.get_sender_id()
        is_admin = str(sender_id) in self.cfg.admins_id
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

        text = event.message_str.partition(" ")[2].strip()
        images = await get_image_urls(event)
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
            text = f"【来自 {sender_name} 的投稿】\n\n{text}" if text else f"【来自 {sender_name} 的投稿】"
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
        """管理员解封用户投稿权限"""
        target = uid or (get_ats(event)[0] if get_ats(event) else "")
        if not target:
            yield event.plain_result("用法：/qq空间_解封投稿 @用户 或 /qq空间_解封投稿 <QQ号>")
            return
        await self.publish_review.clear_strikes(target)
        yield event.plain_result(f"用户 {target} 已解封投稿权限。")

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
        status = "🚫 已封禁" if banned else f"✅ 正常（违规 {strikes}/{self.publish_review.BAN_THRESHOLD} 次）"
        yield event.plain_result(f"用户 {target} 投稿状态：{status}")

    # ---- LLM 工具 ----

    @filter.llm_tool()
    async def llm_publish_feed(self, event: AiocqhttpMessageEvent, text: str = "", get_image: bool = True):
        sender_id = event.get_sender_id()
        is_admin = str(sender_id) in self.cfg.admins_id
        if not bool(self.cfg.trigger.publish_everyone_enabled if self.cfg.trigger.publish_everyone_enabled is not None else True) and not is_admin:
            return "当前只允许管理员发说说。"
        daily_limit = int(self.cfg.trigger.publish_per_user_daily_limit or 1)
        today_count = await self.db.count_interactions_since(
            action="publish", actor_uin=sender_id, since_ts=self._today_start_ts(),
        )
        if daily_limit >= 0 and today_count >= daily_limit and not is_admin:
            return f"你今天已经让 bot 发过 {today_count} 条说说啦，明天再来。"

        images = await get_image_urls(event) if get_image else []
        publish_text = (text or "").strip()
        sender_name = event.get_sender_name() or sender_id

        # 🛡️ 空文本兜底：LLM 有时不传 text 参数，尝试从消息链提取
        if not publish_text:
            for msg in reversed(event.get_messages()):
                if isinstance(msg, Plain) and msg.text.strip():
                    candidate = msg.text.strip()
                    # 过滤纯命令词
                    cmd_words = {"发说说", "投稿", "帮我发", "帮我发说说", "发一条", "发个说说"}
                    if candidate.lower() not in cmd_words and len(candidate) > 2:
                        publish_text = candidate
                        break
            if not publish_text:
                return "发布内容为空。请告诉我你想发什么内容，比如：'帮我发说说 今天天气真好'"

        if not is_admin:
            if self.publish_review.is_banned(str(sender_id)):
                return "你的投稿权限已被限制，无法继续投稿。"
            review = await self.publish_review.submit(
                user_id=str(sender_id), nickname=sender_name,
                text=publish_text, images=images,
            )
            if review.status == review.BANNED:
                return "你的投稿权限已被限制，无法继续投稿。"
            publish_text = review.publish_text
            if not publish_text:
                if review.strikes >= self.publish_review.BAN_THRESHOLD:
                    return "投稿未通过审核，且你已累计多次违规，以后将无法投稿。"
                return "投稿未通过审核，请修改后重新投稿。"
            try:
                post = await self.service.publish_post(text=publish_text, images=images or [])
                await self.db.log_interaction(
                    action="publish", source="llm_publish_approved",
                    tid=post.tid, target_uin=post.uin,
                    group_id=event.get_group_id(), actor_uin=sender_id,
                )
                await self.sender.send_post(event, post, message="投稿审核通过，已发布")
                return "已发布说说到QQ空间。"
            except Exception as e:
                logger.error(f"LLM发说说失败：{e}")
                return f"发布失败：{e}"

        if bool(self.cfg.trigger.publish_with_attribution if self.cfg.trigger.publish_with_attribution is not None else True):
            publish_text = f"【来自 {sender_name} 的投稿】\n\n{publish_text}" if publish_text else f"【来自 {sender_name} 的投稿】"
        try:
            post = await self.service.publish_post(text=publish_text, images=images)
            await self.db.log_interaction(
                action="publish", source="llm_publish",
                tid=post.tid, target_uin=post.uin,
                group_id=event.get_group_id(), actor_uin=sender_id,
            )
            await self.sender.send_post(event, post, message="已发布")
            return "已发布说说到QQ空间。"
        except Exception as e:
            logger.error(f"LLM发说说失败：{e}")
            return f"发布失败：{e}"

    @filter.llm_tool()
    async def llm_visit_friend_qzone(self, event: AiocqhttpMessageEvent, user_id: str | None = None):
        target_id = user_id or event.get_sender_id()
        try:
            posts = await self.service.query_feeds(target_id=target_id, pos=0, num=1, with_detail=True)
        except Exception as e:
            err_msg = str(e)
            logger.warning(f"LLM工具只读访问空间失败: {err_msg}")
            if "Empty" in err_msg or "无权" in err_msg or "不可见" in err_msg or "不存在" in err_msg:
                return "访问失败：对方可能没有开放QQ空间权限，或者内容不可见。"
            return f"访问出错了：{err_msg}"
        if not posts:
            return "访问成功，但是空间是空的，最近没有发说说。"
        post = posts[0]
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
