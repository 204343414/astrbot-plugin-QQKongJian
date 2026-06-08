from __future__ import annotations

import asyncio
import random
import re
import time
import zoneinfo
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from astrbot.api import logger
from config import PluginConfig
from model import Post
from sender import Sender
from service import PostService


# ============================================================
# 定时任务：AutoComment
# Source: core/scheduler.py
# ============================================================

class AutoRandomCronTask:
    """
    基类：在 cron 规定的周期内随机某个时间点执行任务。
    子类只需实现 async do_task()。
    """

    def __init__(self, job_name: str, cron_expr: str, timezone: zoneinfo.ZoneInfo):
        self.timezone = timezone
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()
        self.cron_expr = self._normalize_cron_expr(cron_expr)
        self.job_name = job_name
        self.register_task()
        logger.info(f"[{self.job_name}] 已启动，任务周期：{self.cron_expr}")

    @staticmethod
    def _normalize_cron_expr(cron_expr: str) -> str:
        """兼容把 */8 误写/误存成 /8 或 * /8 的 cron。"""
        expr = re.sub(r"\s+", " ", str(cron_expr or "").strip())
        parts = expr.split(" ")
        if len(parts) == 5:
            parts = [("*" + part) if re.fullmatch(r"/\d+", part) else part for part in parts]
            return " ".join(parts)
        if len(parts) == 6 and parts[0] == "*" and re.fullmatch(r"/\d+", parts[1]):
            return "*" + parts[1] + " " + " ".join(parts[2:])
        return expr

    def register_task(self):
        try:
            self.trigger = CronTrigger.from_crontab(self.cron_expr)
            self.scheduler.add_job(
                func=self.schedule_random_job,
                trigger=self.trigger,
                name=f"{self.job_name}_scheduler",
                max_instances=1,
            )
        except Exception as e:
            logger.error(f"[{self.job_name}] Cron 格式错误：{e}")

    def schedule_random_job(self):
        now = datetime.now(self.timezone)
        next_run = self.trigger.get_next_fire_time(None, now)
        if not next_run:
            logger.error(f"[{self.job_name}] 无法计算下一次周期时间")
            return
        cycle_seconds = int((next_run - now).total_seconds())
        delay = random.randint(0, cycle_seconds)
        target_time = now + timedelta(seconds=delay)
        logger.info(f"[{self.job_name}] 下周期随机执行时间：{target_time}")
        self.scheduler.add_job(
            func=self._run_task_wrapper,
            trigger=DateTrigger(run_date=target_time, timezone=self.timezone),
            name=f"{self.job_name}_once_{target_time.timestamp()}",
            max_instances=1,
        )

    async def _run_task_wrapper(self):
        logger.info(f"[{self.job_name}] 开始执行任务")
        await self.do_task()
        logger.info(f"[{self.job_name}] 本轮任务完成")

    async def do_task(self):
        raise NotImplementedError

    async def terminate(self):
        self.scheduler.remove_all_jobs()
        logger.info(f"[{self.job_name}] 已停止")


class AutoComment(AutoRandomCronTask):
    def __init__(self, config: PluginConfig, service: PostService, sender: Sender):
        cron = config.trigger.comment_cron
        timezone = config.timezone
        super().__init__("AutoComment", cron, timezone)
        self.cfg = config
        self.service = service
        self.sender = sender

    async def do_task(self):
        try:
            posts = await self.service.query_feeds(
                pos=0, num=20, no_self=True, no_commented=False
            )
        except Exception as e:
            logger.error(f"[AutoComment] 获取动态失败：{e}")
            return

        if not posts:
            logger.info("[AutoComment] 没有需要评论的新说说")
            return

        now = datetime.now(self.cfg.timezone)
        today_start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        daily_limit = int(self.cfg.trigger.auto_comment_per_user_daily_limit or 3)
        cooldown_minutes = int(self.cfg.trigger.auto_comment_per_user_cooldown_minutes or 180)
        commented = 0

        for post in posts:
            if not post.tid:
                continue
            if self.cfg.source.is_ignore_user(str(post.uin)):
                continue
            try:
                if await self.service.db.has_interaction(action="space_comment", tid=post.tid):
                    continue
                today_count = await self.service.db.count_interactions_since(
                    action="space_comment",
                    target_uin=post.uin,
                    since_ts=today_start,
                    source_prefix="auto",
                )
                if daily_limit >= 0 and today_count >= daily_limit:
                    continue
                last_ts = await self.service.db.last_interaction_ts(
                    action="space_comment",
                    target_uin=post.uin,
                    source_prefix="auto",
                )
                if last_ts and time.time() - last_ts < cooldown_minutes * 60:
                    continue

                safe_to_forward, unsafe_reason = await self.service.llm.review_post_for_forward(post)
                if not safe_to_forward:
                    logger.warning(f"[AutoComment] 跳过不适合搬运/互动的说说：tid={post.tid}, uin={post.uin}, reason={unsafe_reason}")
                    continue

                await self.service.comment_posts(post)
                await self.service.db.log_interaction(
                    action="space_comment",
                    source="auto_cron",
                    tid=post.tid,
                    target_uin=post.uin,
                )
                if self.cfg.trigger.like_when_comment:
                    try:
                        if await self.service.llm.should_like(post):
                            await self.service.like_posts(post)
                    except Exception as e:
                        logger.error(f"[AutoComment] 点赞判断失败：{e}")
                await self.sender.send_admin_post(post, message="定时读说说：已评论")
                commented += 1
            except Exception as e:
                err_msg = str(e)
                logger.error(f"[AutoComment] 处理说说失败：{e}")
                if "403" in err_msg or "Permission denied" in err_msg or "suspended" in err_msg or "LLM 调用失败" in err_msg:
                    logger.error("[AutoComment] LLM 当前不可用，中止本轮定时评论，避免连续报错。")
                    break
                continue
            await asyncio.sleep(random.randint(3, 10))

        logger.info(f"[AutoComment] 本轮评论了 {commented} 条说说")
