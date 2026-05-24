from __future__ import annotations

import asyncio
import base64
import datetime
import datetime as _dt
import html as html_lib
import json
import random
import re
import shutil
import time
import zoneinfo
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

import aiohttp
import aiosqlite
import bs4
import json5
import pydantic
from aiocqhttp import CQHttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from pydantic import BaseModel

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.config.astrbot_config import AstrBotConfig as _AstrBotConfigForType
from astrbot.core.message.components import At, BaseMessageComponent, Image, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform import AstrMessageEvent as _AstrMessageEventCompat
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.provider.provider import Provider
from astrbot.core.star.context import Context as _ContextForType
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

# 命令前缀说明：filter.command 里不写斜杠，AstrBot 用户侧通常使用 /命令。
CMD_PREFIX = "qq空间_"
BytesOrStr = Union[str, bytes]  # noqa: UP007


# ============================================================
# 配置系统：ConfigNode / PluginConfig
# Source: core/config.py
# ============================================================

# config.py





class ConfigNode:
    """
    配置节点, 把 dict 变成强类型对象。

    规则：
    - schema 来自子类类型注解
    - 声明字段：读写，写回底层 dict
    - 未声明字段和下划线字段：仅挂载属性，不写回
    - 支持 ConfigNode 多层嵌套（lazy + cache）
    """

    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] 缺少字段: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] "
                            f"字段 {key} 期望 dict，实际是 {type(value).__name__}"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        """
        底层配置 dict 的只读视图
        """
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        """
        保存配置到磁盘（仅允许在根节点调用）
        """
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() 只能在根配置节点上调用"
            )
        self._data.save_config()


# ============ 插件自定义配置 ==================


class LLMConfig(ConfigNode):
    comment_provider_id: str
    comment_prompt: str

class SourceConfig(ConfigNode):
    ignore_groups: list[str]
    ignore_users: list[str]
    post_max_msg: int

    def __init__(self, data: MutableMapping[str, Any]):
        super().__init__(data)

    def is_ignore_group(self, group_id: str) -> bool:
        return group_id in self.ignore_groups

    def is_ignore_user(self, user_id: str) -> bool:
        return user_id in self.ignore_users


class TriggerConfig(ConfigNode):
    comment_cron: str
    read_prob: float
    send_admin: bool
    like_when_comment: bool
    auto_read_enabled: bool
    auto_comment_per_user_daily_limit: int
    auto_comment_per_user_cooldown_minutes: int
    group_show_per_user_daily_limit: int
    auto_probe_cooldown_minutes: int
    publish_everyone_enabled: bool
    publish_per_user_daily_limit: int
    publish_with_attribution: bool


class PluginConfig(ConfigNode):
    manage_group: str
    pillowmd_style_dir: str
    llm: LLMConfig
    source: SourceConfig
    trigger: TriggerConfig
    cookies_str: str
    timeout: int

    _DB_VERSION = 4

    def __init__(self, cfg: AstrBotConfig, context: Context):
        super().__init__(cfg)
        self.context = context
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_qqkongjian")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.data_dir / f"posts_{self._DB_VERSION}.db"

        self.default_style_dir = Path(__file__).resolve().parent / "default_style"
        raw_style_dir = str(self.pillowmd_style_dir or "").strip()
        if raw_style_dir:
            candidate_style_dir = Path(raw_style_dir)
            if not candidate_style_dir.is_absolute():
                candidate_style_dir = Path(__file__).resolve().parent / candidate_style_dir
            candidate_style_dir = candidate_style_dir.resolve()
            old_qzone_style = "astrbot_plugin_qzone" in str(candidate_style_dir)
            if old_qzone_style or not candidate_style_dir.exists():
                logger.warning(
                    f"pillowmd_style_dir={candidate_style_dir} 不存在或指向旧插件名，已回退到默认样式 {self.default_style_dir}"
                )
                self.style_dir = self.default_style_dir
            else:
                self.style_dir = candidate_style_dir
        else:
            self.style_dir = self.default_style_dir

        tz = context.get_config().get("timezone")
        self.timezone = (
            zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        )

        self.admins_id: list[str] = context.get_config().get("admins_id", [])
        self._normalize_id()
        self.admin_id = self.admins_id[0] if self.admins_id else None
        self.save_config()

        self.client: CQHttp | None = None

    def _normalize_id(self):
        """仅保留纯数字ID"""
        for ids in [
            self.admins_id,
            self.source.ignore_groups,
            self.source.ignore_users,
        ]:
            normalized = []
            for raw in ids:
                s = str(raw)
                if s.isdigit():
                    normalized.append(s)
            ids.clear()
            ids.extend(normalized)

    def append_ignore_users(self, uid: str | list[str]):
        uids = [uid] if isinstance(uid, str) else uid
        for uid in uids:
            if not self.source.is_ignore_user(uid):
                self.source.ignore_users.append(str(uid))
        self.save_config()

    def remove_ignore_users(self, uid: str | list[str]):
        uids = [uid] if isinstance(uid, str) else uid
        for uid in uids:
            if self.source.is_ignore_user(uid):
                self.source.ignore_users.remove(str(uid))
        self.save_config()

    def update_cookies(self, cookies_str: str):
        self.cookies_str = cookies_str
        self.save_config()


# ============================================================
# 数据模型：Comment / Post
# Source: core/model.py
# ============================================================

def extract_and_replace_nickname(input_string):
    # 匹配{}内的内容，包括非标准JSON格式
    pattern = r"\{[^{}]*\}"

    def replace_func(match):
        content = match.group(0)
        # 按照键值对分割
        pairs = content[1:-1].split(",")
        nick_value = ""
        for pair in pairs:
            if ":" not in pair:
                continue
            key, value = pair.split(":", 1)
            if key.strip() == "nick":
                nick_value = value.strip()
                break
        # 如果找到nick值，则返回@nick_value，否则返回空字符串
        return f"{nick_value} " if nick_value else ""

    return re.sub(pattern, replace_func, input_string)


def remove_em_tags(text):
    """
    移除字符串中的 [em]...[/em] 标记
    :param text: 输入的字符串
    :return: 移除标记后的字符串
    """
    # 使用正则表达式匹配 [em]...[/em] 并替换为空字符串
    cleaned_text = re.sub(r"\[em\].*?\[/em\]", "", text)
    return cleaned_text


class Comment(BaseModel):
    """QQ 空间单条评论（含主评论与楼中楼）"""

    uin: int
    nickname: str
    content: str
    create_time: int
    create_time_str: str = ""
    tid: int = 0
    parent_tid: int | None = None  # 为 None 表示主评论
    source_name: str = ""
    source_url: str = ""

    # 可选：把 create_time 转成 datetime
    @property
    def dt(self) -> _dt.datetime:
        return _dt.datetime.fromtimestamp(self.create_time)

    # 可选：去掉 QQ 内置表情标记 [em]e123[/em]
    @property
    def plain_content(self) -> str:
        return re.sub(r"\[em\]e\d+\[/em\]", "", self.content)

    # ------------------- 工厂方法 -------------------
    @staticmethod
    def from_raw(raw: dict, parent_tid: int | None = None) -> "Comment":  # noqa: UP037
        """单条 dict → Comment（内部使用）"""
        return Comment(
            uin=int(raw.get("uin") or 0),
            nickname=raw.get("name") or "",
            content=raw.get("content") or "",
            create_time=int(raw.get("create_time") or 0),
            create_time_str=raw.get("createTime2") or "",
            tid=int(raw.get("tid") or 0),
            parent_tid=parent_tid,
            source_name=raw.get("source_name") or "",
            source_url=raw.get("source_url") or "",
        )

    @staticmethod
    def build_list(comment_list: list[dict]) -> list["Comment"]:  # noqa: UP037
        """把 emotion_cgi_msgdetail_v6 里的 commentlist 整段 flatten 成 List[Comment]"""
        res: list["Comment"] = []  # noqa: UP037
        for main in comment_list:
            # 主评论
            main_tid = int(main.get("tid") or 0)
            res.append(Comment.from_raw(main, parent_tid=None))
            # 楼中楼
            for sub in main.get("list_3") or []:
                res.append(Comment.from_raw(sub, parent_tid=main_tid))
        return res

    # ------------------- 方便打印 / debug -------------------
    def __str__(self) -> str:
        flag = "└─↩" if self.parent_tid else "●"
        return f"{flag} {self.nickname}({self.uin}): {self.plain_content}"

    def pretty(self, indent: int = 0) -> str:
        """树状缩进打印（仅用于把主/子评论手动分组后展示）"""
        prefix = "  " * indent
        return f"{prefix}{self.nickname}: {self.plain_content}"


class Post(pydantic.BaseModel):
    """稿件"""

    id: int | None = None
    """稿件ID"""
    tid: str | None = None
    """QQ给定的说说ID"""
    uin: int = 0
    """用户ID"""
    name: str = ""
    """用户昵称"""
    gin: int = 0
    """群聊ID"""
    text: str = ""
    """文本内容"""
    images: list[str] = pydantic.Field(default_factory=list)
    """图片列表"""
    videos: list[str] = pydantic.Field(default_factory=list)
    """视频列表"""
    anon: bool = False
    """是否匿名"""
    status: str = "approved"
    """状态"""
    create_time: int = pydantic.Field(
        default_factory=lambda: int(datetime.now().timestamp())
    )
    """创建时间"""
    rt_con: str = ""
    """转发内容"""
    comments: list[Comment] = pydantic.Field(default_factory=list)
    """评论列表"""
    extra_text: str | None = None
    """额外文本"""

    class Config:
        json_encoders = {Comment: lambda c: c.model_dump()}

    def to_str(self) -> str:
        """把稿件信息整理成易读文本"""
        is_pending = self.status == "pending"
        lines = [
            f"### 【{self.id}】{self.name}{'投稿' if is_pending else '发布'}于{datetime.fromtimestamp(self.create_time).strftime('%Y-%m-%d %H:%M')}"
        ]
        if self.text:
            lines.append(f"\n\n{remove_em_tags(self.text)}\n\n")
        if self.rt_con:
            lines.append(f"\n\n[转发]：{remove_em_tags(self.rt_con)}\n\n")
        if self.images:
            images_str = "\n".join(f"  ![图片]({img})" for img in self.images)
            lines.append(images_str)
        if self.videos:
            videos_str = "\n".join(f"  [视频]({vid})" for vid in self.videos)
            lines.append(videos_str)
        if self.comments:
            lines.append("\n\n【评论区】\n")
            for comment in self.comments:
                lines.append(
                    f"- **{remove_em_tags(comment.nickname)}**: {remove_em_tags(extract_and_replace_nickname(comment.content))}"
                )
        if is_pending:
            name = "匿名者" if self.anon else f"{self.name}({self.uin})"
            lines.append(f"\n\n备注：稿件#{self.id}待审核, 投稿来自{name}")

        return "\n".join(lines)

    def update(self, **kwargs):
        """更新 Post 对象的属性"""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                raise AttributeError(f"Post 对象没有属性 {key}")


# ============================================================
# 说说/投稿数据库：PostDB
# Source: core/db.py
# ============================================================

# pots.py




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
POST_KEYS = set(get_args(PostKey))

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
                return int(row[0] if row else 0)

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


# ============================================================
# 用户画像与好感度：UserMemory
# Source: core/user_memory.py
# ============================================================



# ============================================================
# QQ空间基础模型：QzoneContext / ApiResponse
# Source: core/qzone/model.py
# ============================================================

class QzoneContext:
    """统一封装 Qzone 请求所需的所有动态参数"""

    def __init__(self, uin: int, skey: str, p_skey: str, raw_cookies: dict[str, str] | None = None, qzonetoken: str = ""):
        self.uin = uin
        self.skey = skey
        self.p_skey = p_skey
        self.raw_cookies = raw_cookies or {}
        self.qzonetoken = qzonetoken

    @property
    def gtk2(self) -> str:
        """动态计算 gtk2"""
        hash_val = 5381
        for ch in self.p_skey:
            hash_val += (hash_val << 5) + ord(ch)
        return str(hash_val & 0x7FFFFFFF)

    def cookies(self) -> dict[str, str]:
        cookies = dict(self.raw_cookies)
        cookies.update({
            "uin": f"o{self.uin}",
            "skey": self.skey,
            "p_skey": self.p_skey,
        })
        return cookies

    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            "referer": f"https://user.qzone.qq.com/{self.uin}",
            "origin": "https://user.qzone.qq.com",
            "Connection": "keep-alive",
        }



@dataclass(slots=True)
class ApiResponse:
    """
    统一接口响应结果
    """

    ok: bool
    code: int
    message: str | None
    data: dict[str, Any]
    raw: dict[str, Any]

    @classmethod
    def from_raw(
        cls,
        raw: dict[str, Any],
        *,
        code_key: str = "code",
        msg_key: str | tuple[str, ...] = ("message", "msg"),
        data_key: str | None = None,
        success_code: int = 0,
    ) -> "ApiResponse":
        # 解析 code
        code = raw.get(code_key, -1)

        # 解析 message
        message = None
        if isinstance(msg_key, tuple):
            for k in msg_key:
                if raw.get(k):
                    message = raw.get(k)
                    break
        else:
            message = raw.get(msg_key) or raw.get("data", {}).get(msg_key) or code
        # 成功
        if code == success_code:
            data: dict[str, Any] = raw if data_key is None else raw.get(data_key, {})
            return cls(
                ok=True,
                code=code,
                message=None,
                data=data,
                raw=raw,
            )

        # 失败
        return cls(
            ok=False,
            code=code,
            message=message,
            data={},
            raw=raw,
        )

    # -------------------------
    # Python 语义增强
    # -------------------------
    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        if self.ok:
            return f"<ApiResponse ok code={self.code}>"
        return f"<ApiResponse fail code={self.code} message={self.message!r}>"

    # -------------------------
    # 使用辅助
    # -------------------------
    def unwrap(self) -> dict[str, Any]:
        if not self.ok:
            raise RuntimeError(f"{self.code}: {self.message}")
        return self.data or {}

    def get(self, key: str, default: Any = None) -> Any:
        """
        安全访问 data 内字段
        """
        if not self.ok or not self.data:
            return default
        return self.data.get(key, default)

    # -------------------------
    # 调试
    # -------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "data": self.data,
            "raw": self.raw,
        }


# ============================================================
# QQ空间响应解析：QzoneParser
# Source: core/qzone/parser.py
# ============================================================

# parser.py





def _normalize_image_url(url: Any) -> str | None:
    if not url:
        return None
    s = html_lib.unescape(str(url)).strip()
    if not s:
        return None
    if s.startswith("//"):
        s = "https:" + s
    elif s.startswith("http://"):
        # LLM 多数更稳定访问 https；QQ 图片域通常支持 https。
        s = "https://" + s[7:]
    if not (s.startswith("https://") or s.startswith("http://")):
        return None
    return s


def _image_dedupe_key(url: str) -> str:
    s = html_lib.unescape(str(url)).strip()
    # QQ 图片同一资源经常只是 query 参数不同；卡片展示按主体路径去重。
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = s.split("?", 1)[0]
    s = s.split("#", 1)[0]
    return s.rstrip("/").lower()


def _normalize_image_urls(urls: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for url in urls:
        normalized = _normalize_image_url(url)
        if not normalized:
            continue
        key = _image_dedupe_key(normalized)
        if key and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _is_probable_qzone_image_url(url: str) -> bool:
    s = html_lib.unescape(str(url)).strip()
    if not s:
        return False
    low = s.lower()
    if any(bad in low for bad in ("qzonestyle", "emotion", "qlogo", "face", "emoji", "sprite", "icon")):
        return False
    image_domains = (
        "qpic.cn", "m.qpic.cn", "a1.qpic.cn", "a2.qpic.cn", "a3.qpic.cn", "a4.qpic.cn",
        "photo.qq.com", "photovideo.photo.qq.com", "qzone.qq.com/", "gtimg.cn/",
    )
    if any(domain in low for domain in image_domains):
        # video mp4 不当作识图输入，但 video 封面可以由其它字段提供。
        return not low.endswith(".mp4")
    return False


def _extract_image_urls_from_obj(obj: Any) -> list[str]:
    """从 Qzone JSON/HTML 混合结构里递归提取疑似图片 URL。"""
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_l = str(key).lower()
            if isinstance(value, str):
                if ("url" in key_l or "pic" in key_l or "image" in key_l or "cover" in key_l) and _is_probable_qzone_image_url(value):
                    found.append(value)
            elif isinstance(value, (dict, list, tuple)):
                found.extend(_extract_image_urls_from_obj(value))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found.extend(_extract_image_urls_from_obj(item))
    return found


def _extract_image_urls_from_html(html_text: str) -> list[str]:
    urls: list[str] = []
    if not html_text:
        return urls
    soup = bs4.BeautifulSoup(html_text, "html.parser")
    img_attrs = ("src", "data-src", "data-original", "data-url", "data-img", "data-pic", "origin-src", "origin-url", "url")
    for img in soup.find_all("img"):
        for attr in img_attrs:
            raw = img.get(attr)
            if raw and _is_probable_qzone_image_url(str(raw)):
                urls.append(str(raw))
                break
    # 部分动态图在 style="background-image:url(...)" 里。
    for tag in soup.find_all(True):
        style = tag.get("style")
        if style:
            for m in re.finditer(r'url\(([\'"]?)(.*?)\1\)', str(style), re.I):
                raw = m.group(2)
                if raw and _is_probable_qzone_image_url(raw):
                    urls.append(raw)
    # 兜底：直接从 HTML 文本里扫 qpic/photo URL。
    for m in re.finditer(r'https?://[^\'"<>\s]+', html_lib.unescape(html_text)):
        raw = m.group(0)
        if _is_probable_qzone_image_url(raw):
            urls.append(raw)
    return urls


def _safe_cell(text: str, max_len: int = 30) -> str:
    """
    pillowmd-safe 的表格单元格：
    - 无换行
    - 无 |
    - 不为空
    - 长度受限
    """
    if not text:
        return "-"
    text = str(text)
    text = text.replace("\n", " ").replace("|", "｜").strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text or "-"


class QzoneParser:
    """QQ 空间响应解析器"""

    @staticmethod
    def parse_response(text: str, *, debug: bool = False) -> dict[str, Any]:
        """
        解析 JSON / JSONP / 非标准 JSON
        """

        if debug:
            logger.debug(f"响应数据: {text}")

        if m := re.search(
            r"callback\s*\(\s*([^{]*(\{.*\})[^)]*)\s*\)",
            text,
            re.I | re.S,
        ):
            json_str = m.group(2)
        else:
            json_str = text[text.find("{") : text.rfind("}") + 1]

        json_str = json_str.replace("undefined", "null").strip()

        try:
            data = json5.loads(json_str)
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析错误: {e}")
            raise

        if not isinstance(data, dict):
            raise RuntimeError("JSON 解析结果不是 dict")

        if debug:
            logger.debug(f"解析后数据: {data}")

        return data

    @staticmethod
    def parse_upload_result(payload: dict[str, Any]) -> tuple[str, str]:
        data = payload["data"]
        picbo = data["url"].split("&bo=", 1)[1]

        richval = ",{},{},{},{},{},{},,{},{}".format(
            data["albumid"],
            data["lloc"],
            data["sloc"],
            data["type"],
            data["height"],
            data["width"],
            data["height"],
            data["width"],
        )
        return picbo, richval

    @staticmethod
    def parse_visitors(data: dict[str, Any]) -> str:
        data = data.get("data") or {}
        items = data.get("items")

        if not isinstance(items, list) or not items:
            return "### 最近来访明细\n\n暂无访客记录"

        src_map: dict[int, str] = {
            0: "访问空间",
            13: "查看动态",
            32: "手机QQ",
            41: "国际版QQ/TIM",
        }

        lines: list[str] = []

        # ======================
        # 明细表格
        # ======================
        lines.append("\n### 最近来访明细\n")
        lines.append("| 时间 | 访客 | 来源 | 状态 | 带来了 |")
        lines.append("| --- | --- | --- | --- | --- |")

        for v in items:
            if not isinstance(v, dict):
                continue

            # 时间
            ts = v.get("time")
            ts_int = ts if isinstance(ts, int) else 0
            dt = datetime.datetime.fromtimestamp(ts_int).strftime("%m-%d %H:%M")

            # 访客
            name = v.get("name")
            visitor = _safe_cell(name if isinstance(name, str) else "匿名", 16)

            # 来源
            src_val = v.get("src")
            src_key = src_val if isinstance(src_val, int) else -1
            src = _safe_cell(src_map.get(src_key, f"未知({src_key})"), 12)

            # 状态
            status_parts: list[str] = []
            yellow = v.get("yellow")
            if isinstance(yellow, int) and yellow > 0:
                status_parts.append(f"LV{yellow}")
            if v.get("is_hide_visit"):
                status_parts.append("隐身")
            status = _safe_cell(" / ".join(status_parts), 12)

            # 备注（只允许一句）
            remark = "-"

            shuos = v.get("shuoshuoes")
            if isinstance(shuos, list):
                for s in shuos:
                    if isinstance(s, dict):
                        title = s.get("name")
                        if isinstance(title, str) and title.strip():
                            remark = _safe_cell(f"说说:{title}", 30)
                            break

            uins = v.get("uins")
            if remark == "-" and isinstance(uins, list):
                names = []
                for u in uins:
                    if isinstance(u, dict):
                        n = u.get("name")
                        if isinstance(n, str) and n.strip():
                            names.append(n)
                if names:
                    remark = _safe_cell("、".join(names), 30)

            lines.append(
                f"| {_safe_cell(dt, 16)} | {visitor} | {src} | {status} | {remark} |"
            )
        # ======================
        # 表格外统计（底下一行）
        # ======================
        today = data.get("todaycount", 0)
        total = data.get("totalcount", 0)
        lines.append(f"今日访客共 {today} 人， 最近30天访客共 {total} 人")

        return "\n".join(lines)

    @staticmethod
    def parse_feeds(msglist: list[dict]) -> list[Post]:
        """解析说说列表"""
        try:
            posts = []
            for msg in msglist:
                logger.debug(msg)
                # 提取图片信息
                image_urls = []
                for img_data in msg.get("pic", []):
                    for key in ("url2", "url3", "url1", "smallurl"):
                        if raw := img_data.get(key):
                            image_urls.append(raw)
                            break
                # 读取视频封面（按图片处理）
                for video in msg.get("video") or []:
                    video_image_url = video.get("url1") or video.get("pic_url")
                    image_urls.append(video_image_url)
                # 原字段没有提到图时，才递归兜底提取，避免同图多形态污染卡片。
                if not image_urls:
                    image_urls.extend(_extract_image_urls_from_obj(msg))
                # 提取视频播放地址
                video_urls = []
                for video in msg.get("video") or []:
                    url = video.get("url3")
                    if url:
                        video_urls.append(url)
                # 提取转发内容
                rt_con = msg.get("rt_con", {}).get("content", "")
                # 提取评论
                comments = Comment.build_list(msg.get("commentlist") or [])
                # 构造Post对象
                post = Post(
                    tid=msg.get("tid", 0),
                    uin=msg.get("uin", 0),
                    name=msg.get("name", ""),
                    gin=0,
                    text=msg.get("content", "").strip(),
                    images=_normalize_image_urls(image_urls),
                    videos=video_urls,
                    anon=False,
                    status="approved",
                    create_time=msg.get("created_time", 0),
                    rt_con=rt_con,
                    comments=comments,
                    extra_text=msg.get("source_name"),
                )
                posts.append(post)

            return posts

        except Exception as e:
            logger.error(f"解析说说列表失败: {e}")
            return []

    @staticmethod
    def parse_recent_feeds(data: dict) -> list[Post]:
        """解析最近说说列表"""
        feeds: list = data.get("data", {}).get("data", {})
        if not data:
            return []
        try:
            posts = []
            for feed in feeds:
                if not feed:
                    continue
                # 过滤广告类内容（appid=311）
                appid = str(feed.get("appid", ""))
                if appid != "311":
                    continue
                uin = feed.get("uin", "")
                tid = feed.get("key", "")
                if not uin or not tid:
                    logger.error(f"无效的说说数据: target_qq={uin}, tid={tid}")
                    continue
                create_time = feed.get("abstime", "")
                nickname = feed.get("nickname", "")
                html_content = feed.get("html", "")
                if not html_content:
                    logger.error(f"说说内容为空: UIN={uin}, TID={tid}")
                    continue

                soup = bs4.BeautifulSoup(html_content, "html.parser")

                # 提取文字内容
                text_div = soup.find("div", class_="f-info")
                text = text_div.get_text(strip=True) if text_div else ""
                # 提取转发内容
                rt_con = ""
                txt_box = soup.select_one("div.txt-box")
                if txt_box:
                    # 获取除昵称外的纯文本内容
                    rt_con = txt_box.get_text(strip=True)
                    # 分割掉昵称部分（从第一个冒号开始取内容）
                    if "：" in rt_con:
                        rt_con = rt_con.split("：", 1)[1].strip()
                # 提取图片URL：先使用上游原逻辑，只有完全没图时才启用兜底提取。
                image_urls = []
                if img_box := soup.find("div", class_="img-box"):
                    for img in img_box.find_all("img"):  # type: ignore
                        src = img.get("src")  # type: ignore
                        if src and _is_probable_qzone_image_url(str(src)):
                            image_urls.append(src)
                # TODO 临时视频处理办法（视频缩略图）
                img_tag = soup.select_one("div.video-img img")
                if img_tag and "src" in img_tag.attrs:
                    image_urls.append(img_tag["src"])
                if not image_urls:
                    image_urls.extend(_extract_image_urls_from_html(html_content))
                    image_urls.extend(_extract_image_urls_from_obj(feed))
                # 获取视频url
                videos = []
                video_div = soup.select_one("div.img-box.f-video-wrap.play")
                if video_div and "url3" in video_div.attrs:
                    videos.append(video_div["url3"])
                # 获取评论内容
                comments: list[Comment] = []
                # 查找所有评论项（包括主评论和回复）
                comment_items = soup.select("li.comments-item.bor3")
                if comment_items:
                    for item in comment_items:
                        # 提取基本信息
                        data_uin = str(item.get("data-uin", ""))
                        comment_tid = str(item.get("data-tid", ""))
                        nickname = str(item.get("data-nick", ""))

                        # 查找评论内容
                        content_div = item.select_one("div.comments-content")
                        if content_div:
                            # 移除操作按钮（回复/删除）
                            for op in content_div.select("div.comments-op"):
                                op.decompose()
                            # 获取纯文本内容
                            content = content_div.get_text(" ", strip=True).split(
                                ":", 1
                            )[-1]
                        else:
                            content = ""

                        # 提取评论时间（直接使用相对时间字符串）
                        comment_time_span = item.select_one("span.state")
                        comment_time = (
                            comment_time_span.get_text(strip=True)
                            if comment_time_span
                            else ""
                        )

                        # 检查是否是回复
                        parent_tid = None
                        parent_div = item.find_parent("div", class_="mod-comments-sub")
                        if parent_div:
                            parent_li = parent_div.find_parent(
                                "li", class_="comments-item"
                            )
                            if parent_li:
                                parent_tid = str(parent_li.get("data-tid"))  # type: ignore

                        comments.append(
                            Comment(
                                uin=int(data_uin) if data_uin.isdigit() else 0,
                                nickname=nickname,
                                content=content,
                                create_time=0,
                                create_time_str=comment_time,
                                tid=int(comment_tid) if comment_tid.isdigit() else 0,
                                parent_tid=int(parent_tid)
                                if parent_tid and parent_tid.isdigit()
                                else None,
                            )
                        )
                # 构造Post对象
                post = Post(
                    tid=str(tid),
                    uin=int(uin),
                    name=str(nickname),
                    text=text,
                    images=_normalize_image_urls(image_urls),
                    videos=videos,
                    create_time=create_time,
                    rt_con=rt_con,
                    comments=comments,
                )
                posts.append(post)

            logger.info(f"成功解析 {len(posts)} 条最新说说")
            return posts
        except Exception as e:
            logger.error(f"解析说说错误：{e}")
            return []


# ============================================================
# QQ空间登录会话：QzoneSession
# Source: core/qzone/session.py
# ============================================================

# qzone_api.py





class QzoneSession:
    """QQ 登录上下文"""

    DOMAIN = "user.qzone.qq.com"

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self._ctx: QzoneContext | None = None
        self._lock = asyncio.Lock()

    async def get_ctx(self) -> QzoneContext:
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

    async def login(self, cookies_str: str | None = None) -> QzoneContext:
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
        self._ctx = QzoneContext(
            uin=uin,
            skey=c.get("skey", ""),
            p_skey=c.get("p_skey", ""),
            raw_cookies=c,
            qzonetoken=qzonetoken,
        )

        logger.info(f"登录成功，uin={uin}, qzonetoken={'有' if qzonetoken else '无'}")
        return self._ctx


# ============================================================
# QQ空间工具函数：图片下载/归一化
# Source: core/qzone/utils.py
# ============================================================

BytesOrStr = Union[str, bytes]  # noqa: UP007

async def qzone_download_file(url: str) -> bytes | None:
    """下载图片"""
    url = url.replace("https://", "http://")
    try:
        async with aiohttp.ClientSession() as client:
            response = await client.get(url)
            img_bytes = await response.read()
            return img_bytes
    except Exception as e:
        logger.error(f"图片下载失败: {e}")


async def normalize_images(images: Sequence[BytesOrStr] | None) -> list[bytes]:
    """
    将 str/bytes 混合列表统一转成 bytes 列表：
    - str -> 下载后转 bytes（下载失败则忽略）
    - bytes -> 原样保留
    - None -> 空列表
    """
    if images is None:
        return []

    cleaned: list[bytes] = []
    for item in images:
        if isinstance(item, bytes):
            cleaned.append(item)
        elif isinstance(item, str):
            file = await qzone_download_file(item)
            if file is not None:
                cleaned.append(file)
        else:
            raise TypeError(f"image 必须是 str 或 bytes，收到 {type(item)}")
    return cleaned


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
        ctx = await self.session.get_ctx()
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


# ============================================================
# QQ空间API封装：QzoneAPI
# Source: core/qzone/api.py
# ============================================================

class QzoneAPI(QzoneHttpClient):
    """QQ 空间 HTTP API 封装"""

    BASE_URL = "https://user.qzone.qq.com"
    UPLOAD_IMAGE_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
    EMOTION_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
    DOLIKE_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
    LIST_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
    COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    ZONE_LIST_URL = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
    VISITOR_URL = "https://h5.qzone.qq.com/proxy/domain/g.qzone.qq.com/cgi-bin/friendshow/cgi_get_visitor_more"
    REPLY_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    DELETE_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6"
    DETAIL_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6"

    def __init__(self, session: QzoneSession, config: PluginConfig):
        super().__init__(session, config)

    async def _upload_image(self, image: bytes) -> ApiResponse:
        """上传单张图片 (本接口较为脆弱)"""
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "POST",
            self.UPLOAD_IMAGE_URL,
            data={
                "filename": "filename",
                "uploadtype": "1",
                "albumtype": "7",
                "skey": ctx.skey,
                "uin": ctx.uin,
                "p_skey": ctx.p_skey,
                "output_type": "json",
                "base64": "1",
                "picfile": base64.b64encode(image).decode(),
            },
            headers={
                "referer": f"{self.BASE_URL}/{ctx.uin}",
                "origin": self.BASE_URL,
            },
            timeout=60,
        )
        logger.debug(raw)
        return ApiResponse.from_raw(raw, code_key="ret", msg_key="msg")

    async def get_visitor(self) -> ApiResponse:
        """获取访客数"""
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "GET",
            self.VISITOR_URL,
            params={
                "uin": ctx.uin,
                "mask": 7,
                "g_tk": ctx.gtk2,
                "page": 1,
                "fupdate": 1,
                "clear": 1,
            },
        )
        return ApiResponse.from_raw(raw)

    async def publish(self, post: Post) -> ApiResponse:
        """发表说说, 返回tid"""
        ctx = await self.session.get_ctx()
        data: dict[str, Any] = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": post.text,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": "1",
            "to_sign": "0",
            "hostuin": ctx.uin,
            "code_version": "1",
            "format": "json",
            "qzreferrer": f"{self.BASE_URL}/{ctx.uin}",
        }
        if post.images:
            logger.debug(f"正在上传图片: {post.images}")
            pic_bos, richvals = [], []
            imgs: list[bytes] = await normalize_images(post.images)
            for img in imgs:
                resp = await self._upload_image(img)
                if not resp.ok:
                    raise RuntimeError(f"上传图片失败: {resp.message}")
                picbo, richval = QzoneParser.parse_upload_result(resp.data)
                pic_bos.append(picbo)
                richvals.append(richval)
            data.update(
                pic_bo=",".join(pic_bos),
                richtype="1",
                richval="\t".join(richvals),
            )

        raw = await self.request(
            "POST",
            self.EMOTION_URL,
            params={"g_tk": ctx.gtk2, "uin": ctx.uin},
            data=data,
        )
        return ApiResponse.from_raw(raw)

    async def like(self, post: Post) -> ApiResponse:
        """
        点赞指定说说
        """
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "POST",
            self.DOLIKE_URL,
            params={
                "g_tk": ctx.gtk2,
            },
            data={
                "qzreferrer": f"{self.BASE_URL}/{ctx.uin}",  # 来源
                "opuin": ctx.uin,  # 操作者QQ
                "unikey": f"{self.BASE_URL}/{post.uin}/mood/{post.tid}",  # 动态唯一标识
                "curkey": f"{self.BASE_URL}/{post.uin}/mood/{post.tid}",  # 要操作的动态对象
                "appid": 311,  # 应用ID(说说:311)
                "from": 1,  # 来源
                "typeid": 0,  # 类型ID
                "abstime": int(time.time()),  # 当前时间戳
                "fid": post.tid,  # 动态ID
                "active": 0,  # 活动ID
                "format": "json",  # 返回格式
                "fupdate": 1,  # 更新标记
            },
        )
        return ApiResponse.from_raw(raw)

    async def comment(self, post: Post, content: str) -> ApiResponse:
        """
        评论指定说说
        """
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "POST",
            self.COMMENT_URL,
            params={"g_tk": ctx.gtk2},
            data={
                "topicId": f"{post.uin}_{post.tid}__1",  # 说说ID
                "uin": ctx.uin,  # botQQ
                "hostUin": post.uin,  # 目标QQ
                "feedsType": 100,  # 说说类型
                "inCharset": "utf-8",  # 字符集
                "outCharset": "utf-8",  # 字符集
                "plat": "qzone",  # 平台
                "source": "ic",  # 来源
                "platformid": 52,  # 平台id
                "format": "fs",  # 返回格式
                "ref": "feeds",  # 引用
                "content": content,  # 评论内容
            },
        )
        return ApiResponse.from_raw(raw)

    async def reply(
        self,
        post: Post,
        comment: Comment,
        content: str,
    ) -> ApiResponse:
        """回复指定评论"""
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "POST",
            self.REPLY_URL,
            params={
                "g_tk": ctx.gtk2,
            },
            data={
                "topicId": f"{post.uin}_{post.tid}__1",
                "uin": ctx.uin,
                "hostUin": post.uin,
                "feedsType": 100,
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "plat": "qzone",
                "source": "ic",
                "platformid": 52,
                "format": "fs",
                "ref": "feeds",
                "content": content,
                "commentId": comment.tid,
                "commentUin": comment.uin,
                "richval": "",  # 富文本内容
                "richtype": "",  # 富文本类型
                "private": "0",  # 是否私密评论
                "paramstr": "2",
                "qzreferrer": f"https://user.qzone.qq.com/{ctx.uin}/main",  # 来源页
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
                "TE": "trailers",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                "Referer": "https://user.qzone.qq.com/",
                "Origin": "https://user.qzone.qq.com",
            },
        )
        return ApiResponse.from_raw(raw)

    async def delete(self, tid: str) -> ApiResponse:
        """删除指定说说"""
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "POST",
            self.DELETE_URL,
            params={"g_tk": ctx.gtk2},
            data={
                "uin": ctx.uin,
                "topicId": f"{ctx.uin}_{tid}__1",
                "feedsType": 0,
                "feedsFlag": 0,
                "feedsKey": tid,
                "feedsAppid": 311,
                "feedsTime": int(time.time()),
                "fupdate": 1,
                "ref": "feeds",
                "qzreferrer": (
                    "https://user.qzone.qq.com/"
                    f"proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/"
                    f"feeds_html_module?g_iframeUser=1&i_uin={ctx.uin}&i_login_uin={ctx.uin}"
                    "&mode=4&previewV8=1&style=35&version=8"
                    "&needDelOpr=true"
                ),
            },
        )
        return ApiResponse.from_raw(raw)

    async def get_feeds(
        self,
        target_id: str,
        *,
        pos: int = 0,
        num: int = 1,
    ) -> ApiResponse:
        """
        获取指定QQ号的好友说说列表

        Args:
            target_id (str): 目标QQ号。
            pos (int): 起始位置。
            num (int): 要获取的说说数量。
        """
        ctx = await self.session.get_ctx()
        params = {
            "g_tk": ctx.gtk2,
            "uin": target_id,  # 目标QQ
            "ftype": 0,  # 全部说说
            "sort": 0,  # 最新在前
            "pos": pos,  # 起始位置
            "num": num,  # 获取条数
            "replynum": 100,  # 评论数
            "callback": "_preloadCallback",
            "code_version": 1,
            "format": "jsonp",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "notice": 0,
            "cgi_host": "http://taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6",
            "need_comment": 1,
            "need_private_comment": 1,
        }
        if ctx.qzonetoken:
            params["qzonetoken"] = ctx.qzonetoken
        raw = await self.request(
            "GET",
            self.LIST_URL,
            params=params,
        )
        return ApiResponse.from_raw(raw)

    async def get_detail(self, post: Post) -> ApiResponse:
        """
        获取单条说说详情（含完整评论、转发、图片、视频等）
        """
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "GET",
            self.DETAIL_URL,
            params={
                "uin": post.uin,
                "tid": post.tid,
                "format": "jsonp",
                "g_tk": ctx.gtk2,
            },
        )

        return ApiResponse.from_raw(raw)

    async def get_recent_feeds(self, page: int = 1) -> ApiResponse:
        """
        获取自己的好友说说列表，返回已读与未读的说说列表
        """
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "GET",
            self.ZONE_LIST_URL,
            params={
                "uin": ctx.uin,  # QQ号
                "scope": 0,  # 访问范围
                "view": 1,  # 查看权限
                "filter": "all",  # 全部动态
                "flag": 1,  # 标记
                "applist": "all",  # 所有应用
                "pagenum": page,  # 页码, 测试时发现暂时是无效配置
                "aisortEndTime": 0,  # AI排序结束时间
                "aisortOffset": 0,  # AI排序偏移
                "aisortBeginTime": 0,  # AI排序开始时间
                "begintime": 0,  # 开始时间
                "format": "json",  # 返回格式
                "g_tk": ctx.gtk2,  # 令牌
                "useutf8": 1,  # 使用UTF8编码
                "outputhtmlfeed": 1,  # 输出HTML格式
            },
        )
        return ApiResponse.from_raw(raw)


# ============================================================
# LLM动作：写说说/评论/回复/点赞判断
# Source: core/llm_action.py
# ============================================================

class LLMAction:
    def __init__(self, config: PluginConfig, memory: Any | None = None):
        self.cfg = config
        self.context = config.context
        self.memory = memory  # 由外部传进来的 UserMemory 实例

    def _build_context(
        self, round_messages: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """
        把所有回合里的纯文本消息打包成 openai-style 的 user 上下文。
        """
        contexts: list[dict[str, str]] = []
        for msg in round_messages:
            # 提取并拼接所有 text 片段
            text_segments = [
                seg["data"]["text"] for seg in msg["message"] if seg["type"] == "text"
            ]

            text = f"{msg['sender']['nickname']}: {''.join(text_segments).strip()}"
            # 仅当真正说了话才保留
            if text:
                contexts.append({"role": "user", "content": text})
        return contexts

    async def _get_msg_contexts(self, group_id: str) -> list[dict]:
        """获取群聊历史消息"""
        message_seq = 0
        contexts: list[dict] = []
        if not self.cfg.client:
            raise RuntimeError("客户端未初始化")
        while len(contexts) < self.cfg.source.post_max_msg:
            payloads = {
                "group_id": group_id,
                "message_seq": message_seq,
                "count": 200,
                "reverseOrder": True,
            }
            result: dict = await self.cfg.client.api.call_action(
                "get_group_msg_history", **payloads
            )
            round_messages = result["messages"]
            if not round_messages:
                break
            message_seq = round_messages[0]["message_id"]

            contexts.extend(self._build_context(round_messages))
        return contexts

    @staticmethod
    def extract_content(raw: str) -> str:
        start_marker = '"""'
        end_marker = '"""'
        start = raw.find(start_marker) + len(start_marker)
        end = raw.find(end_marker, start)
        if start != -1 and end != -1:
            return raw[start:end].strip()
        return ""

    @staticmethod
    def strip_thinking(text: str) -> str:
        """去除LLM输出中的思考过程（Claude/DeepSeek/Gemini等）"""
        # Claude: <thinking>...</thinking>
        text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL)
        # DeepSeek: <think>...</think>
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

        text = text.strip()
        if not text:
            return text

        # Gemini风格：以**标题**开头的大段思考内容
        if text.startswith("**") or text.startswith("*"):
            text = re.sub(r"\*\*[^*]+\*\*", "", text)
            text = text.strip()

        # 如果文本仍然很长且开头主要是非中文，提取实际中文评论
        if len(text) > 80:
            sample = text[:50]
            cjk_count = len(re.findall(r'[\u4e00-\u9fff]', sample))
            if cjk_count < 3:
                match = re.search(r'[\u4e00-\u9fff]{2,}', text)
                if match:
                    text = text[match.start():]

        return text.strip()

    @staticmethod
    def sanitize_comment_output(text: str) -> str | None:
        """过滤明显不像空间评论的 LLM 输出，避免把模型自述/报错/长解释发到别人空间。"""
        comment = re.sub(r"[\s　]+", "", (text or "")).rstrip("。")
        if not comment:
            return None
        bad_patterns = [
            "我是", "我叫", "模型", "Gemini", "gemini", "ChatGPT", "Claude", "Qwen", "Kimi",
            "API", "平台", "提供服务", "帖子内容", "链接", "粘贴", "请提供", "请将", "无法", "不能",
            "选项", "解释", "作为一个", "AI", "人工智能",
        ]
        if any(p in comment for p in bad_patterns):
            return None
        if "?" in comment or "？" in comment or "吗" in comment:
            return None
        if len(comment) > 36:
            return None
        if len(re.findall(r"[一-鿿]", comment)) < 2:
            return None
        return comment

    @staticmethod
    def is_critical_risk_content(text: str) -> bool:
        """粗筛自伤/想死类内容。命中后绝不使用普通玩梗兜底。"""
        compact = re.sub(r"[\s　]+", "", (text or "")).lower()
        if not compact:
            return False
        strong_keywords = [
            "想死", "想4", "想④", "想亖", "想噶", "想💀", "想☠", "不想活", "不活了",
            "活不下去", "死了算了", "我死了", "想结束生命", "结束自己", "不想存在",
            "不想醒来", "再也不想醒", "撑不下去", "撑不下去了", "坚持不下去",
            "跳楼", "上天台", "割腕", "吞药", "吃药了", "上吊", "卧轨", "自残",
            "遗书", "最后一条", "下辈子", "来世再见",
        ]
        return any(k in compact for k in strong_keywords)

    @staticmethod
    def critical_fallback_comment() -> str:
        """高危内容的温柔短句池：不热线、不说教、不玩梗。"""
        candidates = [
            "抱抱你，先别一个人扛着", "轻轻抱一下，今天已经很努力了", "摸摸你，先慢慢喘口气",
            "抱抱，能走到这里真的不容易", "先抱一下你，别急着否定自己", "摸摸头，今天先别太苛责自己",
            "抱抱你，这一刻先撑过去就好", "抱抱，难受的时候也不用装没事", "先抱抱你，辛苦是真的辛苦",
            "走到这里已经很不容易了", "你已经撑了很久了", "今天能撑着就已经很厉害了",
            "不是你太脆弱，是这阵子真的太难了", "你已经很努力了，真的", "先别急着给自己判死刑",
            "今天先不用变好，先缓一缓", "先别想太远，过完这一小会儿就好", "不用马上振作，先歇一下",
            "不用立刻做决定，先停一停", "慢一点也没关系", "今天先允许自己脆弱一下",
            "看到了，轻轻陪你一下", "这句话看着有点疼，抱抱你", "这会儿先别独自硬扛",
            "我看到你了，先抱一下", "你不是麻烦，真的", "别把自己丢下，先缓缓",
            "今天太难的话，就先活过今天", "别急着和自己翻脸", "先别站到自己的对立面去",
        ]
        return random.choice(candidates)

    @staticmethod
    def fallback_comment(post: Post) -> str:
        """LLM 输出不可用时的短评论兜底。高危内容必须先走温柔池。"""
        raw_text = "\n".join(x for x in [post.text, post.rt_con] if x)
        if LLMAction.is_critical_risk_content(raw_text):
            return LLMAction.critical_fallback_comment()
        candidates = [
            "这张有点东西", "可以，这波挺有感觉", "这下真被你玩明白了", "有点意思", "这波我看懂了",
            "确实挺会", "行，这条挺抓人", "这条可以", "懂了，味儿对了",
        ]
        if post.images:
            candidates = ["这图有点东西", "这张挺有感觉", "这张可以", "懂了，这图会说话", "这画面挺抓人"]
        return random.choice(candidates)

    @staticmethod
    def is_generic_image_comment(comment: str) -> bool:
        compact = re.sub(r"[\s　]+", "", comment or "")
        generic_patterns = [
            "看着很不错", "蛮有意思", "挺有意思", "有点意思", "挺不错", "还不错",
            "这张可以", "这图可以", "很有感觉", "挺有感觉", "挺好看的", "不错不错",
        ]
        return any(p in compact for p in generic_patterns)

    async def _prepare_llm_image_inputs(self, image_urls: list[str], *, max_images: int = 4) -> list[str]:
        """把 QQ 图片先下载到本地，再把本地路径交给 provider，提高 Gemini 等模型识图成功率。"""
        prepared: list[str] = []
        for idx, url in enumerate(image_urls[:max_images]):
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                    "Referer": "https://user.qzone.qq.com/",
                }
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.cfg.timeout)) as client:
                    async with client.get(url, headers=headers) as resp:
                        data = await resp.read()
                        content_type = resp.headers.get("content-type", "")
                if not data or len(data) < 64:
                    logger.warning(f"LLM识图图片下载失败或过小：url={url[:120]}, size={len(data) if data else 0}")
                    prepared.append(url)
                    continue
                ext = ".jpg"
                if "png" in content_type or data.startswith(bytes([0x89, 0x50, 0x4E, 0x47])):
                    ext = ".png"
                elif "webp" in content_type or data.startswith(b"RIFF"):
                    ext = ".webp"
                elif data.startswith(b"GIF"):
                    ext = ".gif"
                safe_name = f"llm_img_{int(time.time() * 1000)}_{idx}{ext}"
                path = self.cfg.cache_dir / safe_name
                path.write_bytes(data)
                prepared.append(str(path))
                logger.info(f"LLM识图图片已下载：{path} ({len(data)} bytes)")
            except Exception as e:
                logger.warning(f"LLM识图图片下载异常，回退URL：{e} url={url[:120]}")
                prepared.append(url)
        return prepared


    async def generate_comment(self, post: Post) -> str | None:
        """根据帖子内容生成评论"""
        provider = (
            self.context.get_provider_by_id(self.cfg.llm.comment_provider_id)
            or self.context.get_using_provider()
        )
        if not isinstance(provider, Provider):
            logger.error("未配置用于文本生成任务的 LLM 提供商")
            return None
        try:
            content = post.text
            if post.rt_con:  # 转发文本
                content += f"\n[转发]\n{post.rt_con}"
            if self.is_critical_risk_content(content):
                comment = self.critical_fallback_comment()
                logger.warning(f"命中高危内容粗筛，使用温柔兜底评论：{comment}")
                return comment

            # 用户画像/好感度链路当前已停用，避免无用数据库和额外 LLM 复杂度。
            profile_prefix = ""
            full_data = None

            if full_data:
                profile = full_data.get("profile") or ""
                favor = full_data.get("favor") or 0

                # 根据好感度给 LLM 一点语气提示（不要直接暴露数值给用户）
                if favor >= 200:
                    favor_desc = "关系：非常亲密，可以很放松、粘人一点。"
                elif favor >= 100:
                    favor_desc = "关系：好朋友，可以适当开玩笑、调侃。"
                elif favor >= 30:
                    favor_desc = "关系：普通熟人，正常友好交流即可。"
                else:
                    favor_desc = "关系：比较陌生，要礼貌一点、稳重一点。"

                profile_prefix = (
                    "## 关于这位用户的内部画像（只用于调整语气，不要直接说出来）：\n"
                    + profile + "\n"
                    + "当前好感度大致判断：" + favor_desc + "\n\n"
                )

            if not content.strip() and post.images:
                content = "【图片说说】这条说说主要由图片构成，请结合图片生成一句自然短评论。"
            elif not content.strip():
                content = "【无文字说说】请生成一句自然、简短、不过度解读的评论。"
            llm_image_urls = _normalize_image_urls(post.images)
            if post.images and not llm_image_urls:
                logger.warning(f"说说含图片但图片URL清洗后为空，原始images={post.images}")
            llm_image_inputs = await self._prepare_llm_image_inputs(llm_image_urls) if llm_image_urls else []
            if llm_image_inputs:
                logger.info(f"评论识图输入：image_count={len(llm_image_inputs)}, first={str(llm_image_inputs[0])[:160]}")
            else:
                logger.warning(f"评论识图输入：image_count=0, tid={post.tid}, text_preview={(post.text or post.rt_con or '')[:80]!r}")
            image_hint = (
                f"\n[图片数量]：{len(llm_image_inputs)}。如果有图片，必须认真识别图片里的具体元素再评论；"
                "例如角色、动物、游戏段位、物品、动作、文字。禁止只说不错/有意思/很好看；看不清也不要反问。"
                if llm_image_inputs else ""
            )
            prompt = (
                profile_prefix
                + "\n[帖子内容]：\n"
                + content
                + image_hint
                + "\n\n[硬性要求]：只输出一句像普通朋友的QQ空间短评论；有图就必须点名图里至少一个具体元素；不要自称AI/模型；不要提平台、链接、帖子内容缺失；不要解释；不要反问；不要超过18个汉字。"
            )

            logger.debug(prompt)
            llm_response = await provider.text_chat(
                system_prompt=self.cfg.llm.comment_prompt,
                prompt=prompt,
                image_urls=llm_image_inputs,
            )
            cleaned = self.strip_thinking(llm_response.completion_text)
            comment = self.sanitize_comment_output(cleaned)
            if llm_image_inputs and comment and self.is_generic_image_comment(comment):
                logger.warning(f"LLM 图片评论过于泛泛，已丢弃：{comment}")
                comment = None
            if not comment:
                comment = self.fallback_comment(post)
                logger.warning(f"LLM 评论输出不合格，已使用兜底评论：{comment}")
            else:
                logger.info(f"LLM 生成的评论：{comment}")
            return comment

        except Exception as e:
            raise ValueError(f"LLM 调用失败：{e}")


    async def should_like(self, post: Post) -> bool:
        """让LLM判断是否应该给这条说说点赞；输出不合格时默认不赞。"""
        content = post.text or ""
        if post.rt_con:
            content += f"\n[转发]\n{post.rt_con}"
        if self.is_critical_risk_content(content):
            logger.info("LLM点赞判断：命中高危内容 -> 不赞")
            return False

        provider = (
            self.context.get_provider_by_id(self.cfg.llm.comment_provider_id)
            or self.context.get_using_provider()
        )
        if not isinstance(provider, Provider):
            logger.warning("未配置LLM提供商，默认不点赞")
            return False
        try:
            if not content.strip() and post.images:
                content = "【图片说说】请结合图片判断是否适合点赞。"
            elif not content.strip():
                content = "【无文字说说】内容信息不足，除非明显积极，否则回答否。"

            prompt = (
                "判断以下QQ空间说说是否适合点赞。\n"
                "必须只回答一个字：是 或 否。\n"
                "以下情况必须回答否：负面情绪、悲伤、生病、去世、事故、抱怨、自嘲、自伤、想死、用户说不要点赞、内容信息不足。\n"
                "正常分享、开心、成果、可爱、日常中性偏积极，才回答是。\n\n"
                f"说说内容：{content}"
            )
            llm_response = await provider.text_chat(prompt=prompt, image_urls=post.images)
            clean_result = self.strip_thinking(llm_response.completion_text)
            compact = re.sub(r"[\s　]+", "", clean_result)
            if compact.startswith("是"):
                logger.info("LLM点赞判断：是 -> 点赞")
                return True
            if compact.startswith("否"):
                logger.info("LLM点赞判断：否 -> 不赞")
                return False
            logger.warning(f"LLM点赞判断输出不合格，默认不赞：{clean_result}")
            return False
        except Exception as e:
            logger.error(f"LLM点赞判断失败：{e}，默认不点赞")
            return False


# ============================================================
# 业务服务层：PostService
# Source: core/service.py
# ============================================================

class PostService:
    """
    Application Service 层
    """

    def __init__(
        self,
        qzone: QzoneAPI,
        session: QzoneSession,
        db: PostDB,
        llm: LLMAction,
    ):
        self.qzone = qzone
        self.session = session
        self.db = db
        self.llm = llm
        # 已点赞的说说tid缓存（防止toggle取消赞）
        self._liked_tids: set[str] = set()

    # ============================================================
    # 业务接口
    # ============================================================

    async def query_feeds(
        self,
        *,
        target_id: str | None = None,
        pos: int = 0,
        num: int = 1,
        with_detail: bool = False,
        no_self: bool = False,
        no_commented: bool = False,
    ) -> list[Post]:
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
            posts: list[Post] = QzoneParser.parse_recent_feeds(resp.data)[
                pos : pos + num
            ]
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
            # 第1层：检查本地数据库（重启后也能防重复）
            if post.tid:
                db_post = await self.db.get(post.tid, key="tid")
                if db_post and any(c.uin == uin for c in db_post.comments):
                    logger.debug(f"数据库记录已评论，跳过：{post.tid}")
                    continue

            # 第2层：检查QQ空间API返回的评论
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

    # ==================== 对外接口 ========================


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
            Comment(
                uin=uin,
                nickname=name,
                content=content,
                create_time=int(time.time()),
                tid=0,
                parent_tid=None,
            )
        )
        await self.db.save(post)
        logger.info(f"评论 -> {post.name}")

        # 用户画像/好感度链路已停用。


    async def publish_post(
        self,
        *,
        post: Post | None = None,
        text: str | None = None,
        images: list | None = None,
    ) -> Post:
        """发表帖子（支持 Post / text / images，但不能为空）"""

        # 参数校验
        if post is None and not text and not images:
            raise ValueError("post、text、images 不能同时为空")

        # 如果没传 post，就自动构造一个
        if post is None:
            uin = await self.session.get_uin()
            name = await self.session.get_nickname()
            post = Post(
                uin=uin,
                name=name,
                text=text or "",
                images=images or [],
            )

        # 发布
        resp = await self.qzone.publish(post)
        if not resp.ok:
            raise RuntimeError(f"发布说说失败：{resp.data}")

        # 回填发布结果
        post.tid = resp.data.get("tid")
        post.status = "approved"
        post.create_time = resp.data.get("now", post.create_time)

        # 持久化
        await self.db.save(post)
        return post



# ============================================================
# 消息发送与渲染：Sender
# Source: core/sender.py
# ============================================================

class Sender:
    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.style = None
        self._load_renderer()

    def _load_renderer(self):
        # 实例化 pillowmd 样式；失败时降级纯文本，但记录明确原因。
        try:
            import pillowmd

            style_dir = Path(self.cfg.style_dir)
            if not style_dir.exists():
                logger.error(f"pillowmd样式目录不存在：{style_dir}，将降级为纯文本")
                self.style = None
                return
            self.style = pillowmd.LoadMarkdownStyles(str(style_dir))
            logger.info(f"pillowmd样式加载成功：{style_dir}")
        except Exception as e:
            self.style = None
            logger.error(f"无法加载pillowmd样式，将降级为纯文本：{e}")

    async def _post_to_seg(self, post: Post) -> BaseMessageComponent:
        post_text = post.to_str()
        if self.style:
            img = await self.style.AioRender(text=post_text, useImageUrl=True)
            img_path = img.Save(self.cfg.cache_dir)
            return Image.fromFileSystem(str(img_path))
        else:
            return Plain(post_text)

    async def _send_to_admins(self, client: CQHttp, obmsg: list[dict]):
        for admin_id in self.cfg.admins_id:
            if admin_id.isdigit():
                try:
                    await client.send_private_msg(user_id=int(admin_id), message=obmsg)
                except Exception as e:
                    logger.error(f"无法反馈管理员：{e}")

    async def _send_to_manage_group(self, client: CQHttp, obmsg: list[dict]) -> bool:
        try:
            await client.send_group_msg(
                group_id=int(self.cfg.manage_group), message=obmsg
            )
            return True
        except Exception as e:
            logger.error(f"无法反馈管理群：{e}")
            return False

    async def _send_to_user(self, client: CQHttp, user_id: int, obmsg: list[dict]):
        try:
            await client.send_private_msg(user_id=int(user_id), message=obmsg)
        except Exception as e:
            logger.error(f"无法通知用户{user_id}：{e}")

    async def _send_to_group(self, client: CQHttp, group_id: int, obmsg: list[dict]):
        try:
            await client.send_group_msg(group_id=int(group_id), message=obmsg)
        except Exception as e:
            logger.error(f"无法通知群聊{group_id}：{e}")

    async def send_admin_post(
        self,
        post: Post,
        *,
        client: CQHttp | None = None,
        message: str = "",
    ):
        """通知管理群或管理员"""
        client = client or self.cfg.client
        if not client:
            logger.error("缺少客户端，无法发送消息")
            return

        chain = []
        if message:
            chain.append(Plain(message))
        post_seg = await self._post_to_seg(post)
        chain.append(post_seg)

        obmsg = await AiocqhttpMessageEvent._parse_onebot_json(MessageChain(chain))

        succ = False
        if self.cfg.manage_group:
            succ = await self._send_to_manage_group(client, obmsg)
        if not succ and self.cfg.admins_id:
            await self._send_to_admins(client, obmsg)

    async def send_user_post(
        self,
        post: Post,
        *,
        client: CQHttp | None = None,
        message: str = "",
    ):
        """通知投稿者"""
        client = client or self.cfg.client
        if not client:
            logger.error("缺少客户端，无法发送消息")
            return

        chain = []
        if message:
            chain.append(Plain(message))
        post_seg = await self._post_to_seg(post)
        chain.append(post_seg)

        obmsg = await AiocqhttpMessageEvent._parse_onebot_json(MessageChain(chain))

        if post.gin:
            await self._send_to_group(client, post.gin, obmsg)
        elif post.uin:
            await self._send_to_user(client, post.uin, obmsg)

    async def send_post(
        self,
        event: AstrMessageEvent,
        post: Post,
        *,
        message: str = "",
        send_admin: bool = False,
    ):
        if send_admin and self.cfg.admin_id:
            event.message_obj.group_id = None  # type: ignore
            event.message_obj.sender.user_id = self.cfg.admin_id

        post_text = post.to_str()

        chain = []

        if message:
            chain.append(Plain(message))

        if self.style:
            try:
                img = await self.style.AioRender(text=post_text, useImageUrl=True)
                img_path = img.Save(self.cfg.cache_dir)
                chain.append(Image.fromFileSystem(str(img_path)))
            except Exception as e:
                logger.error(f"说说卡片渲染失败，已降级纯文本：{e}")
                chain.append(Plain(post_text))
        else:
            chain.append(Plain(post_text))

        await event.send(event.chain_result(chain))

    async def send_msg(
        self,
        event: AstrMessageEvent,
        message: str = "",
    ):
        chain = []

        if self.style:
            try:
                img = await self.style.AioRender(text=message, useImageUrl=True)
                img_path = img.Save(self.cfg.cache_dir)
                chain.append(Image.fromFileSystem(str(img_path)))
            except Exception as e:
                logger.error(f"消息卡片渲染失败，已降级纯文本：{e}")
                chain.append(Plain(message))
        else:
            chain.append(Plain(message))

        await event.send(event.chain_result(chain))


# ============================================================
# AstrBot消息工具函数
# Source: core/utils.py
# ============================================================

BytesOrStr = Union[str, bytes]  # noqa: UP007


def get_ats(event: AiocqhttpMessageEvent) -> list[str]:
    """获取被at者们的id列表,(@增强版)"""
    ats = [str(seg.qq) for seg in event.get_messages()[1:] if isinstance(seg, At)]
    for arg in event.message_str.split(" "):
        if arg.startswith("@") and arg[1:].isdigit():
            ats.append(arg[1:])
    return ats


async def get_nickname(event: AiocqhttpMessageEvent, user_id) -> str:
    """获取指定群友的群昵称或Q名"""
    group_id = event.get_group_id()
    if group_id:
        member_info = await event.bot.get_group_member_info(
            group_id=int(group_id), user_id=int(user_id)
        )
        return member_info.get("card") or member_info.get("nickname")
    else:
        stranger_info = await event.bot.get_stranger_info(user_id=int(user_id))
        return stranger_info.get("nickname")


def resolve_target_id(
    event: AiocqhttpMessageEvent,
    *,
    get_sender: bool = False,
) -> str:
    if at_ids := get_ats(event):
        return at_ids[0]
    return event.get_sender_id() if get_sender else event.get_self_id()


def parse_range(event: AstrMessageEvent) -> tuple[int, int]:
    """
    解析范围参数，返回 (offset, limit)

    用户输入：
    - n        → 第 n 条
    - s~e      → 第 s 到 e 条
    - 其它 / 无 → 第 1 条
    """
    parts = event.message_str.strip().split()
    if not parts:
        return 0, 1

    end = parts[-1]

    # 范围：s~e
    if "~" in end:
        try:
            s, e = end.split("~", 1)
            s_i = int(s)
            e_i = int(e)
            if s_i <= 0 or e_i < s_i:
                raise ValueError
            return s_i - 1, e_i - s_i + 1
        except ValueError:
            return 0, 1

    # 单个数字：n
    try:
        n = int(end)
        if n <= 0:
            raise ValueError
        return n - 1, 1
    except ValueError:
        return 0, 1


async def download_file(url: str) -> bytes | None:
    """下载图片"""
    url = url.replace("https://", "http://")
    try:
        async with aiohttp.ClientSession() as client:
            response = await client.get(url)
            img_bytes = await response.read()
            return img_bytes
    except Exception as e:
        logger.error(f"图片下载失败: {e}")


async def get_image_urls(event: AstrMessageEvent, reply: bool = True) -> list[str]:
    """获取图片url列表"""
    chain = event.get_messages()
    images: list[str] = []
    # 遍历引用消息
    if reply:
        reply_seg = next((seg for seg in chain if isinstance(seg, Reply)), None)
        if reply_seg and reply_seg.chain:
            for seg in reply_seg.chain:
                if isinstance(seg, Image) and seg.url:
                    images.append(seg.url)
    # 遍历原始消息
    for seg in chain:
        if isinstance(seg, Image) and seg.url:
            images.append(seg.url)
    return images


def get_reply_message_str(event: AstrMessageEvent) -> str | None:
    """
    获取被引用的消息解析后的纯文本消息字符串。
    """
    return next(
        (
            seg.message_str
            for seg in event.message_obj.message
            if isinstance(seg, Reply)
        ),
        "",
    )


# ============================================================
# 表白墙/投稿审核：CampusWall
# Source: core/campus_wall.py
# ============================================================



# ============================================================
# 定时任务：AutoComment
# Source: core/scheduler.py
# ============================================================

# ============================
# 基类：随机偏移的周期任务
# ============================


class AutoRandomCronTask:
    """
    基类：在 cron 规定的周期内随机某个时间点执行任务。
    子类只需实现 async do_task()。
    """

    def __init__(self, job_name: str, cron_expr: str, timezone: zoneinfo.ZoneInfo):
        self.timezone = timezone
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()

        self.cron_expr = cron_expr
        self.job_name = job_name

        self.register_task()

        logger.info(f"[{self.job_name}] 已启动，任务周期：{self.cron_expr}")

    # 注册 cron → 触发 schedule_random_job
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

    # 计算当前周期随机时间点，并安排 DateTrigger 执行
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

    # 统一包装（方便打印日志）
    async def _run_task_wrapper(self):
        logger.info(f"[{self.job_name}] 开始执行任务")
        await self.do_task()
        logger.info(f"[{self.job_name}] 本轮任务完成")

    # 子类实现
    async def do_task(self):
        raise NotImplementedError

    async def terminate(self):
        self.scheduler.remove_all_jobs()
        logger.info(f"[{self.job_name}] 已停止")


# ============================
# 自动评论
# ============================


class AutoComment(AutoRandomCronTask):
    def __init__(
        self,
        config: PluginConfig,
        service: PostService,
        sender: Sender,
    ):
        cron = config.trigger.comment_cron
        timezone = config.timezone
        super().__init__("AutoComment", cron, timezone)
        self.cfg = config
        self.service = service
        self.sender = sender

    async def do_task(self):
        # 定时扫好友动态：只负责空间评论/点赞，不负责群展示。
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


# ============================
# 自动发说说
# ============================




# ============================================================
# AstrBot插件入口：QzonePlugin
# Source: main.py
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
        # 已互动的说说tid缓存（防止重复评论）
        self._interacted_tids: set[str] = set()
        # 概率触发锁（防止两条消息同时触发导致重复评论）
        self._prob_lock = asyncio.Lock()
        self._ignore_cleanup_done = False
        self._prob_last_interact_ts: float = 0.0
        self._prob_daily_key: str = ""
        self._prob_daily_count: int = 0
        self._prob_min_interval_sec: int = 30 * 60  # 全局最短间隔：30分钟
        self._prob_daily_limit: int = 5  # 每日最多概率自动互动 5 次

    async def initialize(self):
        """插件加载时触发"""
        await self.db.initialize()
        if not self.auto_comment and self.cfg.trigger.comment_cron:
            self.auto_comment = AutoComment(self.cfg, self.service, self.sender)

    async def terminate(self):
        """插件卸载时"""
        if self.qzone:
            await self.qzone.close()
        if self.auto_comment:
            await self.auto_comment.terminate()
        if self.cfg.cache_dir.exists():
            try:
                shutil.rmtree(self.cfg.cache_dir)
            except Exception as e:
                logger.error(f"清理缓存失败: {e}")

    async def _cleanup_ignore_users_by_friend_list_once(self):
        """客户端可用后，尝试把已成为好友的 QQ 从 ignore_users 移出一次。"""
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
            action="space_comment",
            target_uin=post.uin,
            since_ts=self._today_start_ts(),
            source_prefix="auto",
        )
        if daily_limit >= 0 and today_count >= daily_limit:
            return False
        last_ts = await self.db.last_interaction_ts(
            action="space_comment",
            target_uin=post.uin,
            source_prefix="auto",
        )
        if last_ts and time.time() - last_ts < cooldown_minutes * 60:
            return False
        try:
            bot_uin = await self.session.get_uin()
            if any(c.uin == bot_uin for c in post.comments):
                await self.db.log_interaction(
                    action="space_comment",
                    source="auto_found_existing",
                    tid=post.tid,
                    target_uin=post.uin,
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
            action="group_show",
            target_uin=post.uin,
            group_id=group_id,
            since_ts=self._today_start_ts(),
        )
        return daily_limit < 0 or today_count < daily_limit

    async def _auto_comment_if_allowed(self, post: Post, *, source: str) -> tuple[bool, bool]:
        """返回 (是否评论, 是否点赞)。评论失败时不阻断群展示。"""
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
                    action="space_comment",
                    source=source,
                    tid=post.tid,
                    target_uin=post.uin,
                )
            if self.cfg.trigger.like_when_comment:
                try:
                    if await self.llm.should_like(post):
                        await self.service.like_posts(post)
                        liked = True
                except Exception as e:
                    logger.error(f"自动点赞判断失败：{e}")
        return commented, liked

    async def _find_latest_post_for_group_show(
        self,
        *,
        target_id: str,
        force: bool = False,
    ) -> tuple[Post | None, str]:
        """优先从好友动态流找目标用户最新说说；找不到再 fallback 到个人主页接口。"""
        diagnostics: list[str] = []
        try:
            recent_posts = await self.service.query_feeds(
                pos=0,
                num=30,
                with_detail=False,
                no_self=not force,
                no_commented=False,
            )
            recent_uins = []
            for p in recent_posts:
                recent_uins.append(str(p.uin))
                if str(p.uin) == str(target_id):
                    diagnostics.append(f"好友动态流命中 target_id={target_id}, tid={p.tid}")
                    return p, "\n".join(diagnostics)
            diagnostics.append(
                "好友动态流未命中；最近解析到的uin=" + ",".join(recent_uins[:12])
            )
        except Exception as e:
            diagnostics.append(f"好友动态流读取失败：{e}")

        detail_error = ""
        list_error = ""
        try:
            posts = await self.service.query_feeds(
                target_id=target_id,
                pos=0,
                num=1,
                with_detail=True,
                no_self=not force,
                no_commented=False,
            )
            if posts:
                diagnostics.append(f"个人主页详情接口命中 tid={posts[0].tid}")
                return posts[0], "\n".join(diagnostics)
        except Exception as e:
            detail_error = str(e)
            diagnostics.append(f"个人主页详情读取错误：{detail_error}")

        try:
            posts = await self.service.query_feeds(
                target_id=target_id,
                pos=0,
                num=1,
                with_detail=False,
                no_self=not force,
                no_commented=False,
            )
            if posts:
                diagnostics.append(f"个人主页列表接口命中 tid={posts[0].tid}")
                return posts[0], "\n".join(diagnostics)
        except Exception as e:
            list_error = str(e)
            diagnostics.append(f"个人主页列表读取错误：{list_error}")
            if "不存在" in list_error:
                self.cfg.append_ignore_users(target_id)

        return None, "\n".join(diagnostics)

    async def _show_latest_post_in_group(
        self,
        event: AiocqhttpMessageEvent,
        *,
        target_id: str,
        group_id: str,
        source: str,
        force: bool = False,
    ) -> bool:
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
            await self.sender.send_post(
                event,
                post,
                message=msg,
                send_admin=False if force else self.cfg.trigger.send_admin,
            )
            if not force:
                await self.db.log_interaction(
                    action="group_show",
                    source=source,
                    tid=post.tid,
                    target_uin=post.uin,
                    group_id=str(group_id),
                    actor_uin=event.get_sender_id(),
                )
            return True
        except Exception as e:
            logger.error(f"群展示说说失败：{e}")
            if force:
                await event.send(event.plain_result(f"测试触发展示失败：{e}\n{diagnostics}"))
            return False


    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def prob_read_feed(self, event: AiocqhttpMessageEvent):
        """群聊消息概率触发：评论空间与群展示分开判定。"""
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
                action="auto_probe",
                target_uin=sender_id,
                source_prefix="auto_group_prob",
            )
            if last_probe and time.time() - last_probe < probe_cooldown * 60:
                return
        if self._prob_lock.locked():
            return

        async with self._prob_lock:
            await self.db.log_interaction(
                action="auto_probe",
                source="auto_group_prob",
                target_uin=sender_id,
                group_id=str(group_id),
                actor_uin=sender_id,
            )
            await self._show_latest_post_in_group(
                event,
                target_id=sender_id,
                group_id=str(group_id),
                source="auto_group_prob",
                force=False,
            )


    async def _get_posts(
        self,
        event: AiocqhttpMessageEvent,
        *,
        target_id: str | None = None,
        with_detail: bool = False,
        no_commented=False,
        no_self=False,
    ) -> list[Post]:
        pos, num = parse_range(event)
        at_ids = get_ats(event)
        if not target_id:
            target_id = at_ids[0] if at_ids else None

        if target_id:
            self.cfg.remove_ignore_users(target_id)
        try:
            logger.debug(
                f"正在查询说说： {target_id, pos, num, with_detail, no_commented, no_self}"
            )
            posts = await self.service.query_feeds(
                target_id=target_id,
                pos=pos,
                num=num,
                with_detail=with_detail,
                no_commented=no_commented,
                no_self=no_self,
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

    @filter.command("qq空间_看说说", alias={"qq空间_查看说说"})
    async def view_feed(self, event: AiocqhttpMessageEvent, arg: str | None = None):
        """
        qq空间_看说说 <@群友> <序号>
        """
        posts = await self._get_posts(event, with_detail=True)
        for post in posts:
            await self.sender.send_post(event, post)

    @filter.command("qq空间_评说说", alias={"qq空间_评论说说", "qq空间_读说说"})
    async def comment_feed(self, event: AiocqhttpMessageEvent):
        """qq空间_评说说 <@群友> <序号/范围>  不带参数时随机评论好友说说"""
        ats = get_ats(event)
        parts = event.message_str.strip().split()
        has_args = bool(ats) or len(parts) > 1

        if has_args:
            # 有参数：评论指定用户的说说（原逻辑，已验证能正常点赞+识图）
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
            # 无参数：随机好友互动
            await self._random_friend_interact(event)

    async def _random_friend_interact(self, event: AiocqhttpMessageEvent):
        """随机选一个好友的未互动说说，使用和评说说相同的评论+点赞逻辑"""
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
        friend_ids = [
            fid for fid in friend_ids
            if fid != self_id and not self.cfg.source.is_ignore_user(fid)
        ]

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

            # 本地去重
            if post.tid and post.tid in self._interacted_tids:
                logger.debug(f"跳过已互动的说说：{post.tid}")
                await asyncio.sleep(1)
                continue

            try:
                # 使用和 /评说说 完全相同的评论+点赞逻辑
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
        """qq空间_发说说 <内容> <图片>, 由用户指定内容"""
        sender_id = event.get_sender_id()
        is_admin = str(sender_id) in self.cfg.admins_id
        if not bool(self.cfg.trigger.publish_everyone_enabled if self.cfg.trigger.publish_everyone_enabled is not None else True) and not is_admin:
            yield event.plain_result("当前只允许管理员发说说。")
            return

        daily_limit = int(self.cfg.trigger.publish_per_user_daily_limit or 1)
        today_count = await self.db.count_interactions_since(
            action="publish",
            actor_uin=sender_id,
            since_ts=self._today_start_ts(),
        )
        if daily_limit >= 0 and today_count >= daily_limit and not is_admin:
            yield event.plain_result(f"你今天已经让 bot 发过 {today_count} 条说说啦，明天再来～")
            return

        text = event.message_str.partition(" ")[2].strip()
        images = await get_image_urls(event)
        if bool(self.cfg.trigger.publish_with_attribution if self.cfg.trigger.publish_with_attribution is not None else True):
            sender_name = event.get_sender_name() or sender_id
            text = f"【来自 {sender_name} 的投稿】\n\n{text}" if text else f"【来自 {sender_name} 的投稿】"
        try:
            post = await self.service.publish_post(text=text, images=images)
            await self.db.log_interaction(
                action="publish",
                source="manual_publish",
                tid=post.tid,
                target_uin=post.uin,
                group_id=event.get_group_id(),
                actor_uin=sender_id,
            )
            await self.sender.send_post(event, post, message="已发布")
            event.stop_event()
        except Exception as e:
            yield event.plain_result(str(e))
            logger.error(e)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("qq空间_测试触发")
    async def debug_trigger_read(self, event: AiocqhttpMessageEvent):
        """管理员测试：强制执行一次群展示流程，不受随机概率和展示记录限制。"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("测试触发只能在群聊里使用。")
            return
        at_ids = get_ats(event)
        target_id = at_ids[0] if at_ids else event.get_sender_id()
        ok = await self._show_latest_post_in_group(
            event,
            target_id=target_id,
            group_id=str(group_id),
            source="manual_debug",
            force=True,
        )
        if not ok:
            return

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("qq空间_自检")
    async def self_check(self, event: AiocqhttpMessageEvent):
        """管理员自检：不评论、不点赞、不发说说，只检查关键链路。"""
        lines: list[str] = ["QQ空间插件自检"]

        def ok(name: str, detail: str = ""):
            lines.append(f"☑ {name}" + (f"：{detail}" if detail else ""))

        def bad(name: str, detail: str = ""):
            lines.append(f"☐ {name}" + (f"：{detail}" if detail else ""))

        # 1. 基础配置
        ok("插件已响应", f"群={event.get_group_id() or '私聊'}，发送者={event.get_sender_id()}")
        ok("数据目录", str(self.cfg.data_dir))
        ok("数据库路径", str(self.cfg.db_path))

        # 2. 数据库与 interaction_log
        try:
            await self.db.initialize()
            async with aiosqlite.connect(self.cfg.db_path) as db:
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

        # 3. 渲染器
        if self.sender.style:
            ok("pillowmd渲染器", str(self.cfg.style_dir))
        else:
            bad("pillowmd渲染器", "未加载，消息会降级纯文本；请看日志里的样式路径错误")

        # 4. Qzone 登录态，只读
        try:
            ctx = await self.session.get_ctx()
            uin = ctx.uin
            nick = await self.session.get_nickname()
            ok("QQ空间登录态", f"{nick}({uin}), qzonetoken={'有' if ctx.qzonetoken else '无'}")
        except Exception as e:
            bad("QQ空间登录态", str(e))

        # 5. LLM 评论输出质量 dry-run，不会发到空间
        try:
            sample = Post(uin=0, name="自检", text="洛克王国截图测试，看看这波配置能不能正常评论", images=[])
            comment = await self.llm.generate_comment(sample)
            if comment:
                ok("LLM评论dry-run", comment)
            else:
                bad("LLM评论dry-run", "返回空")
        except Exception as e:
            bad("LLM评论dry-run", str(e))

        # 6. 输出消毒器固定样本
        unsafe = "我是gemini-3.1-flash-lite-preview，目前通过平台为你提供服务，请提供帖子链接"
        sanitized = LLMAction.sanitize_comment_output(unsafe)
        if sanitized is None:
            ok("评论消毒器", "已拦截模型自述样本")
        else:
            bad("评论消毒器", f"未拦截：{sanitized}")

        # 7. 关键配置摘要
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










    @filter.llm_tool()
    async def llm_publish_feed(
        self,
        event: AiocqhttpMessageEvent,
        text: str = "",
        get_image: bool = True,
    ):
        """
        发布一篇说说到QQ空间。普通用户也可触发，但受每日次数限制。
        Args:
            text(string): 要发布的说说内容
            get_image(boolean): 是否获取当前对话中的图片附加到说说里, 默认为True
        """
        sender_id = event.get_sender_id()
        is_admin = str(sender_id) in self.cfg.admins_id
        if not bool(self.cfg.trigger.publish_everyone_enabled if self.cfg.trigger.publish_everyone_enabled is not None else True) and not is_admin:
            return "当前只允许管理员发说说。"
        daily_limit = int(self.cfg.trigger.publish_per_user_daily_limit or 1)
        today_count = await self.db.count_interactions_since(
            action="publish",
            actor_uin=sender_id,
            since_ts=self._today_start_ts(),
        )
        if daily_limit >= 0 and today_count >= daily_limit and not is_admin:
            return f"你今天已经让 bot 发过 {today_count} 条说说啦，明天再来。"

        images = await get_image_urls(event) if get_image else []
        publish_text = (text or "").strip()
        if bool(self.cfg.trigger.publish_with_attribution if self.cfg.trigger.publish_with_attribution is not None else True):
            sender_name = event.get_sender_name() or sender_id
            publish_text = f"【来自 {sender_name} 的投稿】\n\n{publish_text}" if publish_text else f"【来自 {sender_name} 的投稿】"
        try:
            post = await self.service.publish_post(text=publish_text, images=images)
            await self.db.log_interaction(
                action="publish",
                source="llm_publish",
                tid=post.tid,
                target_uin=post.uin,
                group_id=event.get_group_id(),
                actor_uin=sender_id,
            )
            await self.sender.send_post(event, post, message="已发布")
            return "已发布说说到QQ空间。"
        except Exception as e:
            logger.error(f"LLM发说说失败：{e}")
            return f"发布失败：{e}"


    @filter.llm_tool()
    async def llm_visit_friend_qzone(
        self,
        event: AiocqhttpMessageEvent,
        user_id: str | None = None,
    ):
        """
        只读访问指定好友（或自己）的QQ空间，查看最新说说并发送卡片。
        安全约束：此工具不会评论、不会点赞、不会发布内容。

        Args:
            user_id(string): 目标用户的QQ号。如果用户说“我的空间”，则留空（默认为发送者）。如果指明了某人，请输入对方QQ号。
        """
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


