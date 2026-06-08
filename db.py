import json
import time
from collections.abc import Mapping
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel

from astrbot.api import logger
from config import PluginConfig
from model import Comment, Post


# ============================================================
# 说说/投稿数据库：PostDB
# Source: core/db.py
# ============================================================

PostKey = Literal[
    "id",
    "tid",
    "uin",
    "name",
    "gin",
    "status",
    "anon",
    "text",
    "images",
    "videos",
    "create_time",
    "rt_con",
    "comments",
    "extra_text",
]
POST_KEYS = set(PostKey.__args__)  # type: ignore


class PostDB:

    def __init__(self, config: PluginConfig):
        self.db_path = config.db_path

    @staticmethod
    def _row_to_post(row) -> Post:
        return Post(
            id=row[0],
            tid=row[1],
            uin=row[2],
            name=row[3],
            gin=row[4],
            text=row[5],
            images=json.loads(row[6]),
            videos=json.loads(row[7]),
            anon=bool(row[8]),
            status=row[9],
            create_time=row[10],
            rt_con=row[11],
            comments=[Comment.model_validate(c) for c in json.loads(row[12])],
            extra_text=row[13],
        )

    @staticmethod
    def _encode_urls(urls: list[str]) -> str:
        return json.dumps(urls, ensure_ascii=False)

    async def initialize(self):
        """初始化数据库"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tid TEXT UNIQUE,
                    uin INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    gin INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    images TEXT NOT NULL CHECK(json_valid(images)),
                    videos TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(videos)),
                    anon INTEGER NOT NULL CHECK(anon IN (0,1)),
                    status TEXT NOT NULL,
                    create_time INTEGER NOT NULL,
                    rt_con TEXT NOT NULL DEFAULT '',
                    comments TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(comments)),
                    extra_text TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS interaction_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tid TEXT,
                    target_uin TEXT,
                    group_id TEXT,
                    actor_uin TEXT,
                    action TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    extra TEXT NOT NULL DEFAULT ''
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_interaction_tid_action ON interaction_log(tid, action)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_interaction_target_action_time ON interaction_log(target_uin, action, created_at)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_interaction_group_action_time ON interaction_log(group_id, action, created_at)")
            await db.commit()

    async def add(self, post: Post) -> int:
        """添加稿件"""
        async with aiosqlite.connect(self.db_path) as db:
            comment_dicts = [c.model_dump() for c in post.comments]
            cur = await db.execute(
                """
                INSERT INTO posts (tid, uin, name, gin, text, images, videos, anon, status, create_time, rt_con, comments, extra_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post.tid or None,
                    post.uin,
                    post.name,
                    post.gin,
                    post.text,
                    self._encode_urls(post.images),
                    self._encode_urls(post.videos),
                    int(post.anon),
                    post.status,
                    post.create_time,
                    post.rt_con,
                    json.dumps(comment_dicts, ensure_ascii=False),
                    post.extra_text,
                ),
            )
            await db.commit()
            last_id = cur.lastrowid  # 获取自增ID
            assert last_id is not None
            return last_id

    async def get(self, value, key: PostKey = "id") -> Post | None:
        """
        根据指定字段查询一条稿件记录，默认按 id 查询。
        当 key=='id' 且 value==-1 时，返回 id 最大的那一条记录。
        """
        if value is None:
            raise ValueError("必须提供查询值")
        if key not in POST_KEYS:
            raise ValueError(f"不允许的查询字段: {key}")
        async with aiosqlite.connect(self.db_path) as db:
            # 关键判断：-1 代表取最大 ID
            if key == "id" and value == -1:
                query = "SELECT * FROM posts ORDER BY id DESC LIMIT 1"
                async with db.execute(query) as cursor:
                    row = await cursor.fetchone()
                    return self._row_to_post(row) if row else None
            # 普通查询保持原逻辑
            query = f"SELECT * FROM posts WHERE {key} = ? LIMIT 1"
            async with db.execute(query, (value,)) as cursor:
                row = await cursor.fetchone()
                return self._row_to_post(row) if row else None

    async def list(
        self,
        offset: int = 0,
        limit: int = 1,
        *,
        reverse: bool = False,
    ) -> list[Post]:
        """
        批量获取稿件

        offset: 起始偏移（0 表示最早的）
        limit: 数量
        reverse: 是否反转顺序（True = 最新优先）
        """
        if offset < 0 or limit <= 0:
            return []

        order = "DESC" if reverse else "ASC"

        async with aiosqlite.connect(self.db_path) as db:
            query = f"""
                SELECT * FROM posts
                ORDER BY id {order}
                LIMIT ? OFFSET ?
            """
            async with db.execute(query, (limit, offset)) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_post(row) for row in rows]

    async def update(self, post: Post) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            comment_dicts = [c.model_dump() for c in post.comments]
            await db.execute(
                """
                UPDATE posts SET
                    tid = ?, uin = ?, name = ?, gin = ?, text = ?,
                    images = ?, videos = ?, anon = ?, status = ?,
                    create_time = ?, rt_con = ?, comments = ?, extra_text = ?
                WHERE id = ?
                """,
                (
                    post.tid or None,
                    post.uin,
                    post.name,
                    post.gin,
                    post.text,
                    self._encode_urls(post.images),
                    self._encode_urls(post.videos),
                    int(post.anon),
                    post.status,
                    post.create_time,
                    post.rt_con,
                    json.dumps(comment_dicts, ensure_ascii=False),
                    post.extra_text,
                    post.id,
                ),
            )
            await db.commit()

    async def save(self, post: Post) -> int | None:
        """
        保存 Post：
        1. 有 tid → 尝试按 tid 更新
        2. 有 id  → 按 id 更新
        3. 否则   → 新增
        """
        # 1. 优先用 tid 去重
        if post.tid:
            old = await self.get(post.tid, key="tid")
            if old:
                post.id = old.id
                await self.update(post)
                return post.id

        # 2. 有 id 就更新
        if post.id is not None:
            await self.update(post)
            return post.id

        # 3. 新记录
        post.id = await self.add(post)
        return post.id

    async def log_interaction(
        self,
        *,
        action: str,
        source: str,
        tid: str | None = None,
        target_uin: str | int | None = None,
        group_id: str | int | None = None,
        actor_uin: str | int | None = None,
        extra: str = "",
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO interaction_log (tid, target_uin, group_id, actor_uin, action, source, created_at, extra)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(tid) if tid else None,
                    str(target_uin) if target_uin is not None else None,
                    str(group_id) if group_id is not None else None,
                    str(actor_uin) if actor_uin is not None else None,
                    action,
                    source,
                    int(time.time()),
                    extra,
                ),
            )
            await db.commit()

    async def has_interaction(
        self,
        *,
        action: str,
        tid: str | None = None,
        group_id: str | int | None = None,
    ) -> bool:
        if not tid:
            return False
        query = "SELECT 1 FROM interaction_log WHERE tid = ? AND action = ?"
        params: list[Any] = [str(tid), action]
        if group_id is not None:
            query += " AND group_id = ?"
            params.append(str(group_id))
        query += " LIMIT 1"
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(query, params) as cur:
                return await cur.fetchone() is not None

    async def count_interactions_since(
        self,
        *,
        action: str,
        since_ts: int,
        target_uin: str | int | None = None,
        group_id: str | int | None = None,
        actor_uin: str | int | None = None,
        source_prefix: str | None = None,
    ) -> int:
        query = "SELECT COUNT(*) FROM interaction_log WHERE action = ? AND created_at >= ?"
        params: list[Any] = [action, int(since_ts)]
        if target_uin is not None:
            query += " AND target_uin = ?"
            params.append(str(target_uin))
        if group_id is not None:
            query += " AND group_id = ?"
            params.append(str(group_id))
        if actor_uin is not None:
            query += " AND actor_uin = ?"
            params.append(str(actor_uin))
        if source_prefix:
            query += " AND source LIKE ?"
            params.append(source_prefix + "%")
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(query, params) as cur:
                row = await cur.fetchone()
                return int(row[0] if row else 0) if row else 0

    async def last_interaction_ts(
        self,
        *,
        action: str,
        target_uin: str | int | None = None,
        group_id: str | int | None = None,
        source_prefix: str | None = None,
    ) -> int:
        query = "SELECT MAX(created_at) FROM interaction_log WHERE action = ?"
        params: list[Any] = [action]
        if target_uin is not None:
            query += " AND target_uin = ?"
            params.append(str(target_uin))
        if group_id is not None:
            query += " AND group_id = ?"
            params.append(str(group_id))
        if source_prefix:
            query += " AND source LIKE ?"
            params.append(source_prefix + "%")
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(query, params) as cur:
                row = await cur.fetchone()
                return int(row[0] or 0) if row else 0

    async def delete(self, post_id: int) -> int:
        """删除稿件"""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
            await db.commit()
            return cur.rowcount

    async def delete_by_tid(self, tid: str) -> int:
        """按 tid 删除稿件"""
        if not tid:
            return 0
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("DELETE FROM posts WHERE tid = ?", (str(tid),))
            await db.commit()
            return cur.rowcount

    async def list_published_by_actor(
        self,
        actor_uin: str | int,
        *,
        limit: int = 10,
        include_withdrawn: bool = False,
    ) -> list[dict[str, Any]]:
        """
        列出某个用户成功发布过的投稿（按时间倒序，最新在前）。

        数据来源是 interaction_log 里 action='publish' 的记录（actor_uin 即投稿人），
        再 LEFT JOIN posts 拿到正文/图片做预览。

        include_withdrawn=False 时，会排除掉该用户已经撤回过的 tid
        （即存在 action='withdraw' 且同 tid、同 actor 的记录）。

        返回每条：{tid, text, image_count, created_at, withdrawn}
        """
        actor = str(actor_uin)
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT il.tid,
                       MAX(il.created_at) AS pub_time,
                       p.text,
                       p.images
                FROM interaction_log il
                LEFT JOIN posts p ON p.tid = il.tid
                WHERE il.action = 'publish'
                  AND il.actor_uin = ?
                  AND il.tid IS NOT NULL
                GROUP BY il.tid
                ORDER BY pub_time DESC
                LIMIT ?
                """,
                (actor, int(max(1, limit)) * 3),
            ) as cursor:
                rows = await cursor.fetchall()

            # 查出该用户已撤回的 tid 集合
            withdrawn_tids: set[str] = set()
            async with db.execute(
                "SELECT DISTINCT tid FROM interaction_log WHERE action = 'withdraw' AND actor_uin = ? AND tid IS NOT NULL",
                (actor,),
            ) as cursor:
                for r in await cursor.fetchall():
                    if r[0]:
                        withdrawn_tids.add(str(r[0]))

        result: list[dict[str, Any]] = []
        for row in rows:
            tid = str(row[0]) if row[0] else ""
            if not tid:
                continue
            is_withdrawn = tid in withdrawn_tids
            if is_withdrawn and not include_withdrawn:
                continue
            text = row[2] or ""
            try:
                images = json.loads(row[3]) if row[3] else []
            except Exception:
                images = []
            result.append({
                "tid": tid,
                "text": text,
                "image_count": len(images) if isinstance(images, list) else 0,
                "created_at": int(row[1] or 0),
                "withdrawn": is_withdrawn,
            })
            if len(result) >= limit:
                break
        return result

    async def is_published_by_actor(self, actor_uin: str | int, tid: str) -> bool:
        """判断某条 tid 是否确实是该用户投稿发布的（用于撤回鉴权）。"""
        if not tid:
            return False
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """
                SELECT 1 FROM interaction_log
                WHERE action = 'publish' AND actor_uin = ? AND tid = ?
                LIMIT 1
                """,
                (str(actor_uin), str(tid)),
            ) as cur:
                return await cur.fetchone() is not None
