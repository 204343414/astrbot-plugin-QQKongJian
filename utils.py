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
