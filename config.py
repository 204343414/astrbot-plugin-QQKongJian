from __future__ import annotations

import zoneinfo
from collections.abc import Mapping, MutableMapping, Sequence
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.core import AstrBotConfig
from astrbot.core.config.astrbot_config import AstrBotConfig as _AstrBotConfigForType
from astrbot.core.star.context import Context as _ContextForType
from astrbot.core.star.star_tools import StarTools

# ============================================================
# 配置系统：ConfigNode / PluginConfig
# ============================================================


class ConfigNode:
    """
    配置节点, 把 dict 变成强类型对象。
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
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() 只能在根配置节点上调用"
            )
        self._data.save_config()


class LLMConfig(ConfigNode):
    comment_provider_id: str
    comment_prompt: str
    publish_review_prompt: str
    publish_review_prompt_variables: str


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
    # 新增：投稿配图开关
    publish_with_image: bool
    # 新增：自动同意好友申请
    auto_approve_friend_request: bool
    # 新增：成为好友时清理 ignore_users
    ignore_user_cleanup_on_friend_change: bool


class PluginConfig(ConfigNode):
    manage_group: str
    pillowmd_style_dir: str
    llm: LLMConfig
    source: SourceConfig
    trigger: TriggerConfig
    cookies_str: str
    timeout: int

    _DB_VERSION = 4

    def __init__(self, cfg: AstrBotConfig, context):
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

        self.client = None  # CQHttp 实例，由插件加载后注入

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
