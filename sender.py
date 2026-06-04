import re
import shutil
from pathlib import Path
from typing import Any

import aiohttp
import bs4
import pillowmd
from aiocqhttp import CQHttp
from astrbot.api import logger
from astrbot.core.message.components import BaseMessageComponent, Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from config import PluginConfig
from model import Post


# ============================================================
# 消息发送与渲染：Sender
# Source: core/sender.py
# ============================================================

class Sender:
    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.style = None
        self._load_renderer()

    # setting.json 里显式引用 4 个字体；pillowmd 还会默认加载
    # secondFonts: yahei.ttf / unifont.ttf，以及内置默认 smSans.ttf。
    # 有些部署环境的 pillowmd 包没有带 data/fonts，所以这些也要补齐。
    _DEFAULT_FONT_FILES = (
        "OPPOSans-Regular.ttf",
        "OPPOSans-Medium.ttf",
        "仓耳小丸子.ttf",
        "STIXTwoMath-Regular.ttf",
        "yahei.ttf",
        "unifont.ttf",
        "smSans.ttf",
    )

    _SYSTEM_TTF_CANDIDATES = (
        # 注意：pillowmd 内部会用 fontTools.TTFont 读取字体；多数 .ttc
        # 字体集合会报 “specify a font number”，所以这里只兜底 ttf/otf。
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "C:/Windows/Fonts/msyh.ttf",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/arial.ttf",
    )

    def _pillowmd_font_dir(self) -> Path | None:
        try:
            return Path(pillowmd.__file__).resolve().parent / "data" / "fonts"
        except Exception:
            return None

    def _font_candidates_for(self, target_name: str) -> list[Path]:
        candidates: list[Path] = []
        package_font_dir = self._pillowmd_font_dir()
        if package_font_dir and package_font_dir.exists():
            # 先拿 pillowmd 自带字体。它们一定是 pillowmd 自己能读的格式。
            if target_name == "STIXTwoMath-Regular.ttf":
                package_names = ["STIXTwoMath-Regular.ttf", "smSans.ttf", "yahei.ttf", "unifont.ttf"]
            elif target_name in {"yahei.ttf", "unifont.ttf", "smSans.ttf"}:
                package_names = [target_name, "smSans.ttf", "yahei.ttf", "unifont.ttf"]
            else:
                package_names = ["smSans.ttf", "yahei.ttf", "unifont.ttf"]
            candidates.extend(package_font_dir / name for name in package_names)

        candidates.extend(Path(path) for path in self._SYSTEM_TTF_CANDIDATES)
        return candidates

    def _find_existing_font(self, target_name: str) -> Path | None:
        for path in self._font_candidates_for(target_name):
            if path.exists() and path.is_file() and path.suffix.lower() in {".ttf", ".otf"}:
                return path
        return None

    def _prepare_runtime_default_style(self, style_dir: Path) -> Path:
        """
        default_style 仓库里不再携带大字体文件时，pillowmd 在渲染阶段会
        ImageFont.truetype(...)->cannot open resource。这里不把字体重新塞回仓库，
        而是在 AstrBot 数据目录生成一个运行时样式副本，并用系统字体补齐
        setting.json 里引用的 4 个字体文件名。
        """
        if style_dir.resolve() != self.cfg.default_style_dir.resolve():
            return style_dir

        fonts_dir = style_dir / "fonts"
        missing_fonts = [name for name in self._DEFAULT_FONT_FILES if not (fonts_dir / name).exists()]
        if not missing_fonts:
            return style_dir

        runtime_style_dir = self.cfg.data_dir / "default_style_runtime"
        runtime_fonts_dir = runtime_style_dir / "fonts"
        try:
            shutil.copytree(style_dir, runtime_style_dir, dirs_exist_ok=True)
            runtime_fonts_dir.mkdir(parents=True, exist_ok=True)

            for name in self._DEFAULT_FONT_FILES:
                src = self._find_existing_font(name)
                dst = runtime_fonts_dir / name
                if src and (not dst.exists() or dst.stat().st_size == 0):
                    shutil.copyfile(src, dst)

            still_missing = [name for name in self._DEFAULT_FONT_FILES if not (runtime_fonts_dir / name).exists()]
            if still_missing:
                logger.warning(f"default_style 缺少字体，且未找到可用替代字体 {still_missing}；将尝试原样加载并可能降级纯文本")
                return style_dir

            logger.info(f"default_style 缺少字体 {missing_fonts}，已用系统字体生成运行时样式：{runtime_style_dir}")
            return runtime_style_dir
        except Exception as e:
            logger.warning(f"生成运行时样式失败，将尝试原样加载并可能降级纯文本：{e}")
            return style_dir

    def _load_renderer(self):
        try:
            style_dir = Path(self.cfg.style_dir)
            if not style_dir.exists():
                logger.error(f"pillowmd样式目录不存在：{style_dir}，将降级为纯文本")
                self.style = None
                return
            style_dir = self._prepare_runtime_default_style(style_dir)
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
            await client.send_group_msg(group_id=int(self.cfg.manage_group), message=obmsg)
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

    async def send_admin_post(self, post: Post, *, client: CQHttp | None = None, message: str = ""):
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

    async def send_user_post(self, post: Post, *, client: CQHttp | None = None, message: str = ""):
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

    async def send_post(self, event: AstrMessageEvent, post: Post, *,
                         message: str = "", send_admin: bool = False):
        if send_admin and self.cfg.admin_id:
            event.message_obj.group_id = None
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

    async def send_msg(self, event: AstrMessageEvent, message: str = ""):
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
