#!/usr/bin/env python3
"""
一键合并部署脚本
把多模块 .py 文件合并成单个 main.py，方便直接覆盖部署到 AstrBot
用法: python3 build.py
"""

import os
import re
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent

# 合并顺序（按依赖关系排列）
MODULES = [
    "config.py",
    "model.py",
    "db.py",
    "parser.py",
    "qzone_session.py",
    "qzone_client.py",
    "qzone_api.py",
    "utils.py",
    "llm_action.py",
    "service.py",
    "sender.py",
    "scheduler.py",
    "publish_review.py",
    "main.py",
]

# 需要从最终输出中移除的 import 语句（因为都合并到一个文件里了）
REMOVE_IMPORTS = [
    "from config import",
    "from model import",
    "from db import",
    "from parser import",
    "from qzone_session import",
    "from qzone_client import",
    "from qzone_api import",
    "from utils import",
    "from llm_action import",
    "from service import",
    "from sender import",
    "from scheduler import",
    "from publish_review import",
]

# 文件头注释
HEADER = '''"""
astrbot_plugin_qqkongjian - QQ空间插件（单文件部署版）
自动生成，请勿手动编辑。开发请编辑源模块文件后运行 build.py

模块结构（开发时）：
├── config.py            ← 配置系统
├── model.py             ← 数据模型
├── db.py                ← 数据库层
├── parser.py            ← QQ空间响应解析
├── qzone_session.py     ← 登录会话
├── qzone_client.py      ← HTTP客户端
├── qzone_api.py         ← QQ空间API封装
├── utils.py             ← 通用工具函数
├── llm_action.py        ← LLM动作
├── service.py           ← 业务服务层
├── sender.py            ← 消息发送与渲染
├── scheduler.py         ← 定时任务
├── publish_review.py    ← 投稿审核 (LLM审 + 黑名单)
└── main.py              ← 插件入口 & AstrBot 路由
"""

'''

def merge_modules():
    """合并所有模块到单个 main.py"""
    output_parts = [HEADER]

    for module_name in MODULES:
        module_path = PLUGIN_DIR / module_name
        if not module_path.exists():
            print(f"⚠️  警告: {module_name} 不存在，跳过")
            continue

        content = module_path.read_text(encoding="utf-8")

        # 添加模块分隔注释
        output_parts.append(f"\n# {'='*60}\n# 模块: {module_name}\n# {'='*60}\n\n")

        # 移除内部模块 import（保留第三方和 astrbot import）
        lines = content.split("\n")
        filtered_lines = []
        skip_next_empty = False
        for line in lines:
            should_remove = False
            for rm_import in REMOVE_IMPORTS:
                if line.startswith(rm_import) or line.startswith(f"from .{rm_import.split(' ')[1]}"):
                    should_remove = True
                    break
            if should_remove:
                skip_next_empty = True
                continue
            # 跳过连续空行
            if skip_next_empty and line.strip() == "":
                skip_next_empty = False
                continue
            skip_next_empty = False
            filtered_lines.append(line)

        output_parts.append("\n".join(filtered_lines))
        output_parts.append("\n")

    # 写入合并后的 main.py
    output_path = PLUGIN_DIR / "dist" / "main.py"
    output_path.parent.mkdir(exist_ok=True)
    output_text = "\n".join(output_parts)

    # 清理多余空行（连续 3 个以上空行变成 1 个）
    output_text = re.sub(r"\n{4,}", "\n\n\n", output_text)

    output_path.write_text(output_text, encoding="utf-8")

    # 统计
    line_count = output_text.count("\n")
    size_mb = len(output_text.encode("utf-8")) / 1024 / 1024
    print(f"✅ 合并完成: {output_path}")
    print(f"   行数: {line_count}")
    print(f"   大小: {size_mb:.1f} MB")

    # 同时生成一个压缩包（方便下载）
    import zipfile
    zip_path = PLUGIN_DIR / "dist" / "astrbot_plugin_qqkongjian.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(output_path, "main.py")
        # 添加配置文件
        for conf_file in ["_conf_schema.json", "metadata.yaml", "requirements.txt", "README.md"]:
            src = PLUGIN_DIR / conf_file
            if src.exists():
                zf.write(src, conf_file)
        # 添加 default_style 目录
        style_dir = PLUGIN_DIR / "default_style"
        if style_dir.exists():
            for root, dirs, files in os.walk(style_dir):
                for file in files:
                    fp = Path(root) / file
                    arcname = "default_style" / fp.relative_to(style_dir)
                    zf.write(fp, str(arcname))
        # 添加 logo.png
        logo = PLUGIN_DIR / "logo.png"
        if logo.exists():
            zf.write(logo, "logo.png")

    print(f"✅ 压缩包生成: {zip_path}")
    print(f"   大小: {zip_path.stat().st_size / 1024:.0f} KB")
    print(f"\n📦 部署方法:")
    print(f"   1. 下载 dist/astrbot_plugin_qqkongjian.zip")
    print(f"   2. 解压到 AstrBot/data/plugins/astrbot_plugin_qqkongjian/")
    print(f"   3. 重启 AstrBot")


if __name__ == "__main__":
    merge_modules()
