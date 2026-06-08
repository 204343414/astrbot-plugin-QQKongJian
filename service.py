from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger
from config import PluginConfig
from model import Comment, Post
from parser import QzoneParser
from qzone_api import QzoneAPI
from qzone_session import QzoneSession
from db import PostDB
from llm_action import LLMAction


# ============================================================
# 业务服务层：PostService
# Source: core/service.py
# ============================================================

class PostService:
    """Application Service 层"""

    def __init__(self, qzone: QzoneAPI, session: QzoneSession, db: PostDB, llm: LLMAction):
        self.qzone = qzone
        self.session = session
        self.db = db
        self.llm = llm
        self._liked_tids: set[str] = set()

    async def query_feeds(self, *, target_id: str | None = None, pos: int = 0,
                           num: int = 1, with_detail: bool = False,
                           no_self: bool = False, no_commented: bool = False) -> list[Post]:
        if target_id:
            resp = await self.qzone.get_feeds(target_id, pos=pos, num=num)
            if not resp.ok:
                raise RuntimeError(resp.message)
            msglist = resp.data.get("msglist") or []
            if not msglist:
                raise RuntimeError("查询结果为空")
            posts: list[Post] = QzoneParser.parse_feeds(msglist)
        else:
            resp = await self.qzone.get_recent_feeds()
            if not resp.ok:
                raise RuntimeError(resp.message)
            posts: list[Post] = QzoneParser.parse_recent_feeds(resp.data)[pos : pos + num]
            if not posts:
                raise RuntimeError("查询结果为空")

        if no_self:
            uin = await self.session.get_uin()
            posts = [p for p in posts if p.uin != uin]

        if with_detail:
            posts = await self._fill_post_detail(posts)
            if not posts:
                raise RuntimeError("获取详情后无有效说说")

        if no_commented:
            posts = await self._filter_not_commented(posts)

        for post in posts:
            await self.db.save(post)

        return posts

    async def _fill_post_detail(self, posts: list[Post]) -> list[Post]:
        result: list[Post] = []
        for post in posts:
            resp = await self.qzone.get_detail(post)
            if not resp.ok or not resp.data:
                logger.warning(f"获取详情失败：{resp.data}")
                continue
            parsed = QzoneParser.parse_feeds([resp.data])
            if not parsed:
                logger.warning(f"解析详情失败：{resp.data}")
                continue
            result.append(parsed[0])
        return result

    async def _filter_not_commented(self, posts: list[Post]) -> list[Post]:
        result: list[Post] = []
        uin = await self.session.get_uin()
        for post in posts:
            if post.tid:
                db_post = await self.db.get(post.tid, key="tid")
                if db_post and any(c.uin == uin for c in db_post.comments):
                    logger.debug(f"数据库记录已评论，跳过：{post.tid}")
                    continue
            if not post.comments:
                resp = await self.qzone.get_detail(post)
                if not resp.ok or not resp.data:
                    continue
                parsed = QzoneParser.parse_feeds([resp.data])
                if not parsed:
                    continue
                post = parsed[0]
            if any(c.uin == uin for c in post.comments):
                continue
            result.append(post)
        return result

    async def like_posts(self, post: Post):
        """点赞帖子（防止重复调用导致toggle取消赞）"""
        if not post.tid:
            raise ValueError("帖子 tid 为空")
        if post.tid in self._liked_tids or await self.db.has_interaction(action="space_like", tid=post.tid):
            logger.debug(f"跳过已点赞的说说：{post.tid}（{post.name}）")
            return
        await self.qzone.like(post)
        self._liked_tids.add(post.tid)
        await self.db.log_interaction(action="space_like", source="service", tid=post.tid, target_uin=post.uin)
        logger.info(f"已点赞 → {post.name}")

    async def comment_posts(self, post: Post):
        """评论帖子"""
        if not post.tid:
            raise ValueError("帖子 tid 为空")
        content = await self.llm.generate_comment(post)
        if not content:
            raise ValueError("生成评论内容为空")
        await self.qzone.comment(post, content)
        uin = await self.session.get_uin()
        name = await self.session.get_nickname()
        post.comments.append(
            Comment(uin=uin, nickname=name, content=content,
                    create_time=int(time.time()), tid=0, parent_tid=None)
        )
        await self.db.save(post)
        logger.info(f"评论 -> {post.name}")

    async def withdraw_post(self, tid: str) -> None:
        """
        撤回（删除）一条自己空间的说说。

        说明：QzoneAPI.delete 内部使用登录账号自身的 uin 构造 topicId，
        因此只能删除 bot 账号自己空间里的说说——这正好覆盖“投稿发布到 bot 空间”
        的场景。鉴权（谁能撤谁的稿）在上层（main.py）依据 interaction_log 判断。
        """
        if not tid:
            raise ValueError("tid 为空，无法撤回")
        resp = await self.qzone.delete(str(tid))
        if not resp.ok:
            raise RuntimeError(f"撤回说说失败：{resp.message or resp.data}")
        # 同步清理本地缓存里的该条说说
        try:
            await self.db.delete_by_tid(str(tid))
        except Exception as e:
            logger.warning(f"撤回后清理本地记录失败（不影响撤回结果）：{e}")

    async def publish_post(self, *, post: Post | None = None, text: str | None = None,
                            images: list | None = None) -> Post:
        """发表帖子（支持 Post / text / images，但不能为空）"""
        if post is None and not text and not images:
            raise ValueError("post、text、images 不能同时为空")
        if post is None:
            uin = await self.session.get_uin()
            name = await self.session.get_nickname()
            post = Post(uin=uin, name=name, text=text or "", images=images or [])
        resp = await self.qzone.publish(post)
        if not resp.ok:
            raise RuntimeError(f"发布说说失败：{resp.data}")
        post.tid = resp.data.get("tid")
        post.status = "approved"
        post.create_time = resp.data.get("now", post.create_time)
        await self.db.save(post)
        return post
