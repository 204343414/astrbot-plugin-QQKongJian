# astrbot_plugin_qqkongjian 交接说明

## 当前目标
这是 QQ空间插件的单 `main.py` 维护版。当前只保留核心社交功能：

- `/qq空间_看说说`
- `/qq空间_评说说`
- `/qq空间_发说说`
- `/qq空间_测试触发`（管理员诊断）
- `/qq空间_自检`（管理员诊断）
- LLM 工具：`llm_publish_feed`、`llm_visit_friend_qzone`
- 群聊概率读说说
- 定时扫好友空间 `AutoComment`

已物理删除：用户画像/好感度、投稿墙/审核、查看访客、回评、删说说、独立点赞命令、自动发说说 AutoPublish、AI写稿链路。

## 当前文件
需要部署/交接的文件：

- `main.py`
- `_conf_schema.json`
- `metadata.yaml`
- `requirements.txt`
- `default_style/`
- `logo.png`
- `MAINTENANCE_LOG.txt`
- `SOURCE_MAP.txt`
- `HANDOFF.md`

## 重要维护原则
1. 不要重新引入多文件 `core/` import。单文件版 `main.py` 顶部不应出现 `from .core...`。
2. 维护历史写 `MAINTENANCE_LOG.txt`，不要把长篇思路塞回 `main.py`。
3. 按 `SOURCE_MAP.txt` 定位 `# Source: xxx.py` 区块，等价于分目录维护。
4. 图片解析部分要参考上游源码优先，不要再全量递归提图污染卡片。当前逻辑是：上游原逻辑先提图；只有没图时才启用兜底。

## 当前最重要未完问题：识图
现状：
- 图片数量/去重已修复。
- `generate_comment()` 会记录 `评论识图输入：image_count=...`。
- Round 22 已把 QQ 图片下载到本地 cache，再把本地路径传给 `provider.text_chat(image_urls=...)`。

下一步测试重点：
1. 触发一条带图片的说说评论。
2. 看日志是否出现：
   - `LLM识图图片已下载：.../cache/llm_img_xxx.jpg`
   - `评论识图输入：image_count=1, first=/.../cache/llm_img_xxx.jpg`
3. 如果仍出现“请上传图片”，说明 AstrBot provider 或当前平台没有正确处理 `image_urls` 的本地路径。下一步应考虑 base64 输入或研究 AstrBot provider 的图片传入方式。

## QQ空间接口现状
- `user.qzone.qq.com/proxy/domain/...msglist...` 触发过 WAF 501。
- `h5.qzone.qq.com/...msglist_v6` 无有效 qzonetoken 时返回“使用人数过多，请稍后再试”。
- 浏览器里 `window.g_qzonetoken` 为 `undefined`，源码里曾看到 `g_qzonetoken = ''`。
- 因此群展示目前优先走好友动态流 `get_recent_feeds()`，找不到才 fallback 个人主页接口。

## 当前安全机制
- `interaction_log` 记录 `space_comment`、`space_like`、`group_show`、`publish`、`auto_probe`。
- 同一条说说不重复评论。
- 同一条说说同一群不重复展示。
- 同一人同群每日展示限制。
- 同一人自动评论每日限制和冷却。
- `auto_probe_cooldown_minutes` 防止 read_prob 高时对同一人空间反复 F5。
- D 档高危内容（想死/想4/想💀等）直接走温柔话术池，不走普通兜底，不点赞。
- D 档群展示文案：`看到这条有点心疼，大家温柔一点`。

## 当前建议配置
- `read_prob`: 日常可用 `0.119`，测试不要长期设 `1.0`。
- `auto_probe_cooldown_minutes`: 默认 5，若 QQ空间风控再调高。
- `auto_comment_per_user_daily_limit`: 3 或按用户习惯。
- `auto_comment_per_user_cooldown_minutes`: 180。
- `group_show_per_user_daily_limit`: 3。
- `publish_everyone_enabled`: true。
- `publish_per_user_daily_limit`: 1。
