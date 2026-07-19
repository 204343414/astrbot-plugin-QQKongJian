from __future__ import annotations

import random
import re
import time
from typing import Any

import aiohttp
from pydantic import BaseModel

from astrbot.api import logger
from astrbot.core.provider.provider import Provider
from config import PluginConfig
from model import Comment, Post
from utils import normalize_images


# ============================================================
# LLM动作：写说说/评论/回复/点赞判断
# Source: core/llm_action.py
# ============================================================

class LLMAction:
    def __init__(self, config: PluginConfig, memory: Any | None = None):
        self.cfg = config
        self.context = config.context
        self.memory = memory

    def _build_context(self, round_messages: list[dict[str, Any]]) -> list[dict[str, str]]:
        contexts: list[dict[str, str]] = []
        for msg in round_messages:
            text_segments = [
                seg["data"]["text"] for seg in msg["message"] if seg["type"] == "text"
            ]
            text = f"{msg['sender']['nickname']}: {''.join(text_segments).strip()}"
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
        text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL)
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        text = text.strip()
        if not text:
            return text
        if text.startswith("**") or text.startswith("*"):
            text = re.sub(r"\*\*[^*]+\*\*", "", text)
            text = text.strip()
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
        """过滤明显不像空间评论的 LLM 输出"""
        comment = re.sub(r"[\s ]+", "", (text or "")).rstrip("。")
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
        compact = re.sub(r"[\s ]+", "", (text or "")).lower()
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
        """高危内容的温柔短句池"""
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
        """LLM 输出不可用时的短评论兜底"""
        raw_text = "\n".join(x for x in [post.text, post.rt_con] if x)
        if LLMAction.is_critical_risk_content(raw_text):
            return LLMAction.critical_fallback_comment()
        candidates = [
            "今天状态不错呀", "这条看着挺开心的", "有被温柔到", "生活气息满满",
            "看着心情也跟着好了", "这样的日常挺好的", "味道对了", "挺有生活感的",
        ]
        if post.images:
            candidates = [
                "画面里有种安静的好看", "这光线挺舒服的", "看着像一个好天气",
                "这样的瞬间值得留一张", "颜色搭配得挺舒服", "图里有种温柔的味道",
            ]
        return random.choice(candidates)

    @staticmethod
    def is_generic_image_comment(comment: str) -> bool:
        compact = re.sub(r"[\s ]+", "", comment or "")
        generic_patterns = [
            "看着很不错", "蛮有意思", "挺不错", "还不错", "不错不错",
            "很有感觉", "挺有感觉", "挺好看的",
        ]
        return any(p in compact for p in generic_patterns)

    async def _prepare_llm_image_inputs(self, image_urls: list[str], *, max_images: int = 4) -> list[str]:
        """把 QQ 图片先下载到本地，再把本地路径交给 provider"""
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
            if post.rt_con:
                content += f"\n[转发]\n{post.rt_con}"
            if self.is_critical_risk_content(content):
                comment = self.critical_fallback_comment()
                logger.warning(f"命中高危内容粗筛，使用温柔兜底评论：{comment}")
                return comment

            profile_prefix = ""
            full_data = None

            if not content.strip() and post.images:
                content = "【图片说说】这条说说主要由图片构成，请结合图片生成一句自然短评论。"
            elif not content.strip():
                content = "【无文字说说】请生成一句自然、简短、不过度解读的评论。"
            llm_image_urls = post.images  # already strings
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


    async def review_post_for_forward(self, post: Post) -> tuple[bool, str]:
        """
        审核一条外部 QQ 空间说说是否适合被 bot 搬运/展示/自动互动。
        对含图说说保守处理：真人/写实人物/自拍/擦边/暴露/疑似未成年等一律不搬。
        """
        content = "\n".join(x for x in [post.text, post.rt_con] if x).strip()
        if self.is_critical_risk_content(content):
            return False, "命中自伤/高危情绪内容"

        provider = (
            self.context.get_provider_by_id(self.cfg.llm.comment_provider_id)
            or self.context.get_using_provider()
        )
        has_images = bool(post.images)
        has_videos = bool(post.videos)
        if not isinstance(provider, Provider):
            if has_images:
                return False, "LLM不可用，含图说说默认不搬"
            return True, "无图文本，LLM不可用时放行"

        # 无图普通文本不额外消耗审核；只拦明显风险词。
        compact = re.sub(r"[\s ]+", "", content).lower()
        text_risk_words = [
            "裸照", "色情", "约炮", "外围", "博彩", "赌博", "加微信", "加vx",
            "刷单", "兼职赚钱", "转账", "人肉", "开盒", "网暴",
        ]
        if not has_images and not any(w in compact for w in text_risk_words):
            return True, "无图普通文本"

        llm_image_inputs = await self._prepare_llm_image_inputs(post.images, max_images=4) if has_images else []
        image_rule = (
            "\n图片审核要求：只要图片中存在真人/写实人物/自拍/清晰人脸/未成年人/泳装内衣/暴露身体/性感姿势/肢体特写/擦边暗示/色情低俗/血腥暴力/政治敏感画面/二维码或广告引流，就必须不通过。"
            "二次元、游戏截图、风景、宠物、美食等非擦边内容可以通过；但如果二次元/游戏图有明显擦边、暴露、性暗示，也必须不通过。看不清或不确定时按不通过。"
            if has_images else ""
        )
        prompt = (
            "你是 QQ 群聊内容安全审核员。判断下面这条 QQ 空间说说是否适合被 bot 搬运展示到群聊、自动评论或点赞。\n"
            "审核要保守，任何可能导致 bot 风控/封号/群聊不适的内容都不通过。\n"
            "文本风险包括：违法暴力、色情低俗、擦边引流、广告营销、诈骗、赌博、毒品、人肉网暴、政治敏感、自伤高危等。"
            f"{image_rule}\n\n"
            f"说说作者：{post.name}({post.uin})\n"
            f"说说文字：{content or '（无文字）'}\n"
            f"图片数量：{len(post.images)}\n"
            f"视频数量：{len(post.videos)}\n"
            "（注意：如果含有视频内容，请结合提示词判断是否适合搬运；视频内容无法直接审核，需特别谨慎。）\n\n"
            "请只回答：\n通过\n或\n不通过|简短原因"
        )
        try:
            resp = await provider.text_chat(
                system_prompt="你是严格的群聊内容安全审核员。只输出审核结论，不要解释多余内容。",
                prompt=prompt,
                image_urls=llm_image_inputs,
            )
            result = self.strip_thinking(resp.completion_text).strip()
            compact_result = re.sub(r"[\s ]+", "", result)
            if compact_result.startswith("通过") and not compact_result.startswith("不通过"):
                msg = "LLM审核通过"
                if has_videos:
                    msg += "（检测到视频内容，无法直接审核视频安全性，已由提示词判断是否适合搬运）"
                return True, msg
            if compact_result.startswith("不通过"):
                parts = result.split("|", 1)
                reason = parts[1].strip() if len(parts) > 1 else "LLM审核不通过"
                if has_videos:
                    reason += "（检测到视频内容，无法直接审核视频安全性，提示词已处理拒绝逻辑）"
                return False, reason
            if "不通过" in compact_result or "拒绝" in compact_result or "违规" in compact_result:
                return False, "LLM审核不通过"
            logger.warning(f"搬运安全审核返回不可解析：{result!r}，按不通过处理")
            return False, "审核结果不可解析"
        except Exception as e:
            logger.error(f"搬运安全审核异常：{e}")
            if has_images:
                return False, f"审核异常，含图说说默认不搬: {e}"
            return True, f"审核异常，无图文本放行: {e}"

    async def should_like(self, post: Post) -> bool:
        """让LLM判断是否应该给这条说说点赞"""
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
            compact = re.sub(r"[\s ]+", "", clean_result)
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
