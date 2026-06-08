# astrbot_plugin_qqkongjian 交接说明

## 模块结构
```
main.py              ← 插件入口 & AstrBot 路由（Star 子类 + 命令装饰器）
config.py            ← 配置系统（ConfigNode / PluginConfig）
model.py             ← 数据模型（Comment / Post / QzoneContext / ApiResponse）
db.py                ← 数据库层（PostDB）
parser.py            ← QQ空间响应解析（QzoneParser）
qzone_session.py     ← QQ空间登录会话（QzoneSession）
qzone_client.py      ← QQ空间HTTP客户端（QzoneHttpClient）
qzone_api.py         ← QQ空间API封装（QzoneAPI）
utils.py             ← 通用工具函数（图片下载、消息解析等）
llm_action.py        ← LLM动作（评论生成/点赞判断/高危内容检测）
service.py           ← 业务服务层（PostService）
sender.py            ← 消息发送与渲染（Sender）
scheduler.py         ← 定时任务（AutoComment / AutoRandomCronTask）
publish_review.py    ← 投稿审核（PublishReview）⭐ 新增
```

## 新增功能：投稿审核（publish_review.py）

### 工作原理
1. 普通用户发说说 → 走 LLM 审核
2. 关键词粗筛 → LLM 细审 → 审核通过才发布
3. 审核通过的内容自动加 "【来自 xxx 的投稿】" 标注
4. 管理员发说说 → 跳过审核，直接发布
5. **黑名单机制**：
   - 违规一次记一次 strike
   - 累计超过 3 次（BAN_THRESHOLD）→ 永久拉黑
   - 拉黑用户无法再投稿
   - 黑名单存储在 `publish_bans.db`（独立的 SQLite 文件）
   - **黑名单不写在明文规则里**（用户无感知）

### 管理员命令
- `/qq空间_封投稿 @用户` — 手动封禁用户投稿权限
- `/qq空间_解封投稿 @用户` — 解封用户投稿权限
- `/qq空间_审核状态 @用户` — 查看用户投稿违规次数

### 审核流程
```
用户投稿
  ↓
黑名单检查（直接拦截已拉黑用户）
  ↓
关键词粗筛（极速拦截明显违规）
  ↓
LLM 细审（判断封号风险）
  ↓
审核通过 → 自动加标注 → 发布
审核不通过 → 记违规 → 返回拒绝
```

## 撤回投稿（Round 25 新增）

### 能力
- 普通用户可以撤回**自己投稿过**的说说；管理员可撤回任意说说。
- 撤回前会先列出该用户的投稿（时间 / 内容摘要 / tid），由用户确认撤哪条。
- 鉴权依据 `interaction_log` 里 `action='publish'` 的 `actor_uin`（谁投的谁能撤）。
- 撤回成功后会记 `action='withdraw'`，并清理本地 posts 缓存。

### 管理员/用户命令
- `/qq空间_我的投稿`（别名 `/qq空间_投稿列表`）— 列出自己最近发布成功的投稿及 tid
- `/qq空间_撤回投稿`（别名 `/qq空间_撤稿`、`/qq空间_删投稿`）
  - 无参数 → 列出投稿引导选择
  - `<序号>` → 撤回列表第 N 条
  - `<tid>` → 按 tid 撤回（管理员可撤任意 tid）

### LLM 工具
- `llm_list_my_publishes` — 只读，列出当前用户投稿清单，供 LLM 念给用户确认
- `llm_withdraw_feed(tid)` — 撤回指定 tid；普通用户仅限自己投稿过的，管理员任意

## 审核超时 Bug 修复（Round 25）

**问题**：旧逻辑里大模型超时 / 异常 / provider 不可用都会让 `_llm_review`
返回 `approved=False`，而 `submit()` 把任何非通过都当作违规 `add_strike`，
于是“审核超时 3 次”就会触发永久拉黑（BAN_THRESHOLD=3）。

**修复**：`ReviewResult` 新增 `ERROR` 状态。`_llm_review` 在系统侧失败时返回
`error=True`；`submit()` 命中 error 时**只返回 ERROR**——不记违规、不计入拉黑阈值、
不进冷却（用户可立即重试）。只有 LLM 真正判定“内容违规”才会 `add_strike`。
上层 `publish_feed` / `llm_publish_feed` 对 ERROR 提示“审核服务暂时不可用，没有记你违规，请稍后再试”。

## 维护原则
1. **保守修改**：尽量不动已有功能，优先新增模块
2. **单文件不超千行**：每个 .py 文件保持 500-1000 行以内
3. **模块职责清晰**：db只管DB，parser只管解析，service只管业务逻辑
4. **import 保持一致**：所有同级模块用 `from xxx import yyy`（无点前缀）
5. **维护日志写文件**：不在 main.py 里写长篇注释，而是记录到对应模块

## QQ空间接口现状
- `user.qzone.qq.com/proxy/domain/...msglist...` 触发过 WAF 501
- `h5.qzone.qq.com/...msglist_v6` 无有效 qzonetoken 时返回"使用人数过多"
- 群展示优先走好友动态流 `get_recent_feeds()`，找不到再 fallback 个人主页接口

## 当前安全机制
- `interaction_log` 记录 space_comment、space_like、group_show、publish、auto_probe
- 同一条说说不重复评论
- 同一条说说同一群不重复展示
- 同一人同群每日展示限制
- 同一人自动评论每日限制和冷却
- auto_probe_cooldown_minutes 防止 read_prob 高时对同一人空间反复 F5
- D档高危内容（想死/想4/想💀等）直接走温柔话术池，不走普通兜底，不点赞
- 投稿审核：关键词粗筛 + LLM细审 + 黑名单机制
