from __future__ import annotations

import base64
import json
import random
import re
import shutil
from http.cookies import SimpleCookie
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints
from collections.abc import Mapping, MutableMapping, Sequence
from datetime import datetime, timedelta
import datetime as _dt
import html as html_lib
import time
import zoneinfo

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
from astrbot.core.star.star_tools import StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, BaseMessageComponent, Image, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.provider.provider import Provider
from astrbot.core.star.context import Context as _ContextForType

BytesOrStr = Union[str, bytes]


# ============================================================
# QQ空间工具函数：图片下载/归一化
# Source: core/qzone/utils.py
# ============================================================

async def qzone_download_file(url: str, timeout: int = 30) -> bytes | None:
    """下载图片"""
    url = url.replace("https://", "http://")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as client:
            response = await client.get(url)
            img_bytes = await response.read()
            return img_bytes
    except Exception as e:
        logger.error(f"图片下载失败: {e}")
        return None


async def normalize_images(images: Sequence[BytesOrStr] | None, timeout: int = 30) -> list[bytes]:
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
            file = await qzone_download_file(item, timeout)
            if file is not None:
                cleaned.append(file)
        else:
            raise TypeError(f"image 必须是 str 或 bytes，收到 {type(item)}")
    return cleaned


# ============================================================
# AstrBot消息工具函数
# Source: core/utils.py
# ============================================================

# ------------------------------------------------------------
# QQ空间正文“可点击/可提醒”的 @好友 富文本标记
#
# 依据：腾讯手Q空间接口约定，说说/评论正文（con 字段）里 @某人要写成
#   @{uin:QQ号,nick:昵称}
# 来源1：open.mobile.qq.com Qzone TopicComment 文档明确写
#        “@某人要转成 : @{uin:183852032,nick:diaodiao}”
# 来源2：QzoneExporter 解析历史说说时用的正则同款：
#        @\{uin:(\d+?),nick:(.+?),.*?\}
# 也就是说：这不是一个单独的 API，而是把这段标记直接拼进正文，
# QQ空间渲染时就会变成蓝色、可点击、会提醒对方的 @好友。
# ------------------------------------------------------------

_QZONE_AT_TEMPLATE = "@{{uin:{uin},nick:{nick}}}"
# 纯文本 @QQ号（5~11 位数字）。后面不能紧跟数字，避免吃掉长串数字。
_PLAINTEXT_AT_DIGITS_RE = re.compile(r"@(\d{5,11})(?!\d)")


def sanitize_qzone_nick(nick: str) -> str:
    """
    清洗将要塞进 @{uin:..,nick:..} 的昵称：
    去掉会破坏 {} 结构或可被用来伪造标记的字符（{ } 换行 CQ码等），
    逗号换成全角逗号以免截断 nick 字段。
    """
    raw = str(nick or "")
    raw = re.sub(r"\[CQ:[^\]]+\]", "", raw)
    raw = re.sub(r"[\r\n\t]+", " ", raw)
    raw = raw.replace("{", "").replace("}", "").replace(",", "，")
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:24]


def make_qzone_at(uin, nick: str = "") -> str:
    """
    生成 QQ空间正文里可点击、会提醒对方的 @好友标记。
    - uin 为有效 QQ 号 → 返回 @{uin:QQ号,nick:昵称}
    - 没有有效 QQ 号    → 退回纯文本 @昵称（不可点击，但至少不丢信息）
    """
    uin_digits = re.sub(r"\D", "", str(uin or ""))
    safe_nick = sanitize_qzone_nick(nick) or (uin_digits or "好友")
    if not uin_digits:
        return f"@{safe_nick}"
    return _QZONE_AT_TEMPLATE.format(uin=uin_digits, nick=safe_nick)


async def _resolve_member_name(event: AiocqhttpMessageEvent, uin: str) -> str:
    # 先按群名片/群昵称查（get_nickname）。如果被 @ 的人不在本群，
    # get_group_member_info 会抛异常，此时再用 get_stranger_info 查全局昵称兜底，
    # 尽量避免昵称查不到而回退成 QQ 号（卡片上显示成一串数字很难看）。
    try:
        name = await get_nickname(event, uin)
        if name:
            return name
    except Exception as e:
        logger.debug(f"按群成员解析 @ 对象昵称失败 uin={uin}: {e}")
    try:
        info = await event.bot.get_stranger_info(user_id=int(uin))
        return (info.get("nickname") or "").strip()
    except Exception as e:
        logger.debug(f"按陌生人解析 @ 对象昵称失败 uin={uin}: {e}")
        return ""


async def build_at_map(event: AiocqhttpMessageEvent) -> dict[str, str]:
    """
    从消息里的原生 At 段构建 {uin: 显示名} 映射；昵称缺失时联网补齐。
    @全体成员 (qq=all/0) 不纳入。
    """
    at_map: dict[str, str] = {}
    for seg in event.get_messages():
        if isinstance(seg, At):
            uin = str(seg.qq)
            if uin.lower() in ("all", "0", ""):
                continue
            name = (getattr(seg, "name", "") or "").strip()
            if not name:
                name = await _resolve_member_name(event, uin)
            at_map[uin] = name
    return at_map


async def convert_ats_to_qzone(event: AiocqhttpMessageEvent, text: str,
                               at_map: dict[str, str] | None = None) -> str:
    """
    把正文里的 @ 转成 QQ空间可点击 @好友格式。处理两类：
    1. 纯文本 @QQ号               → @{uin:QQ号,nick:查到的昵称}
    2. @某昵称（昵称来自原生At段） → @{uin:对应QQ号,nick:昵称}
    防伪造：先打断用户原文里可能存在的 @{...} 结构，避免有人手敲标记 @ 到任意人。
    """
    if not text:
        return text
    # 防止用户自己拼 @{uin:...} 伪造他人 @
    text = text.replace("@{", "@ {")

    if at_map is None:
        at_map = await build_at_map(event)

    # 1) 纯文本 @QQ号
    def _digit_sub(m: re.Match) -> str:
        uin = m.group(1)
        return make_qzone_at(uin, at_map.get(uin, ""))

    text = _PLAINTEXT_AT_DIGITS_RE.sub(_digit_sub, text)

    # 2) @昵称（仅限消息里真的 @ 过的人，避免误伤普通 @文字）
    for uin, name in at_map.items():
        if not name:
            continue
        # 后面不接 } 或字母数字，避免改到刚生成的 @{...} 或更长的词
        pattern = re.compile(r"@" + re.escape(name) + r"(?![}\w])")
        text = pattern.sub(make_qzone_at(uin, name), text)

    return text


def strip_command_prefix(text: str, keywords: Sequence[str]) -> str:
    """去掉重建正文时残留的前导命令词（命令词出现在最前面才剥离）。"""
    if not text:
        return text
    for kw in keywords:
        idx = text.find(kw)
        if 0 <= idx <= 5:  # 允许前面有 / # ! 唤醒符或空格
            return text[idx + len(kw):].lstrip()
    return text


def _convert_plaintext_at_digits(text: str, at_map: dict[str, str]) -> str:
    """只把纯文本 @QQ号 转成可点击 @好友（不做防伪造打断，供已处理过 At 段的场景复用）。"""
    if not text:
        return text

    def _digit_sub(m: re.Match) -> str:
        uin = m.group(1)
        return make_qzone_at(uin, at_map.get(uin, ""))

    return _PLAINTEXT_AT_DIGITS_RE.sub(_digit_sub, text)


async def build_command_publish_text(event: AiocqhttpMessageEvent,
                                     command_keywords: Sequence[str]) -> str:
    """
    针对斜杠命令发说说：从消息链重建正文，把原生 At 段就地转成
    QQ空间可点击 @好友，保留它在句子里的位置（message_str 会丢掉 At，所以不能用它）。

    注意顺序：防伪造打断（@{ -> @ {）和纯文本 @QQ号 转换都只作用在“用户输入的
    Plain 文本”上；由 At 段生成的 @{uin:..} 标记是可信的，不再二次处理，避免被打断。
    """
    at_map = await build_at_map(event)
    parts: list[str] = []
    for seg in event.get_messages():
        if isinstance(seg, Plain):
            # 用户原文：先打断伪造的 @{...}，再转纯文本 @QQ号
            chunk = (seg.text or "").replace("@{", "@ {")
            chunk = _convert_plaintext_at_digits(chunk, at_map)
            parts.append(chunk)
        elif isinstance(seg, At):
            uin = str(seg.qq)
            if uin.lower() in ("all", "0", ""):
                parts.append("@全体成员")  # 投稿不真正 @全体，仅保留文字
                continue
            name = (getattr(seg, "name", "") or "").strip() or at_map.get(uin) or await _resolve_member_name(event, uin)
            parts.append(make_qzone_at(uin, name))  # 可信标记，不再二次处理
        # 图片、引用等忽略
    text = "".join(parts)
    text = strip_command_prefix(text, command_keywords)
    return text.strip()


def get_ats(event: AiocqhttpMessageEvent) -> list[str]:
    """获取被at者们的id列表"""
    ats = [str(seg.qq) for seg in event.get_messages()[1:] if isinstance(seg, At)]
    for arg in event.message_str.split(" "):
        if arg.startswith("@") and arg[1:].isdigit():
            ats.append(arg[1:])
    return ats


async def get_nickname(event: AiocqhttpMessageEvent, user_id) -> str:
    """获取指定群友的群昵称或Q名"""
    group_id = event.get_group_id()
    if group_id:
        member_info = await event.bot.get_group_member_info(group_id=int(group_id), user_id=int(user_id))
        return member_info.get("card") or member_info.get("nickname")
    else:
        stranger_info = await event.bot.get_stranger_info(user_id=int(user_id))
        return stranger_info.get("nickname")


def resolve_target_id(event: AiocqhttpMessageEvent, *, get_sender: bool = False) -> str:
    if at_ids := get_ats(event):
        return at_ids[0]
    return event.get_sender_id() if get_sender else event.get_self_id()


def parse_range(event: AstrMessageEvent) -> tuple[int, int]:
    """解析范围参数，返回 (offset, limit)"""
    parts = event.message_str.strip().split()
    if not parts:
        return 0, 1
    end = parts[-1]
    if "~" in end:
        try:
            s, e = end.split("~", 1)
            s_i = int(s); e_i = int(e)
            if s_i <= 0 or e_i < s_i:
                raise ValueError
            return s_i - 1, e_i - s_i + 1
        except ValueError:
            return 0, 1
    try:
        n = int(end)
        if n <= 0:
            raise ValueError
        return n - 1, 1
    except ValueError:
        return 0, 1


async def download_file(url: str, timeout: int = 30) -> bytes | None:
    """下载图片"""
    url = url.replace("https://", "http://")
    try:
        async with aiohttp.ClientSession() as client:
            response = await client.get(url, timeout=aiohttp.ClientTimeout(total=timeout))
            return await response.read()
    except Exception as e:
        logger.error(f"图片下载失败: {e}")
        return None


async def get_image_urls(event: AstrMessageEvent, reply: bool = True) -> list[str]:
    """获取图片url列表"""
    chain = event.get_messages()
    images: list[str] = []
    if reply:
        reply_seg = next((seg for seg in chain if isinstance(seg, Reply)), None)
        if reply_seg and reply_seg.chain:
            for seg in reply_seg.chain:
                if isinstance(seg, Image) and seg.url:
                    images.append(seg.url)
    for seg in chain:
        if isinstance(seg, Image) and seg.url:
            images.append(seg.url)
    return images


def get_reply_message_str(event: AstrMessageEvent) -> str | None:
    """获取被引用的消息解析后的纯文本消息字符串"""
    return next(
        (seg.message_str for seg in event.message_obj.message if isinstance(seg, Reply)),
        "",
    )
