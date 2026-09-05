"""Telegram content parser bot MVP.

This first version intentionally keeps platform parsing behind a small adapter
interface. It verifies channel membership and classifies public links, but does
not collect login cookies or bypass platform access controls.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from content_parsers import fetch_public_metadata

LOG = logging.getLogger("tg-parser-bot")
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
MENU_BUTTONS = [["视频解析", "钱包授权查询"], ["A股分析", "频道入口"]]


@dataclass(frozen=True)
class PlatformLink:
    platform: str
    url: str


def detect_platform(text: str) -> Optional[PlatformLink]:
    """Detect a supported public link without fetching or downloading it."""
    match = URL_RE.search(text or "")
    if not match:
        return None
    url = match.group(0).rstrip("。，、！？)）]")
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if host in {"douyin.com", "www.douyin.com", "v.douyin.com", "iesdouyin.com", "www.iesdouyin.com"}:
        return PlatformLink("抖音", url)
    if host in {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com", "www.xhslink.com"}:
        return PlatformLink("小红书", url)
    return PlatformLink("其他链接", url)


def required_channel() -> str:
    channel = os.getenv("REQUIRED_CHANNEL", "").strip()
    if not channel:
        raise RuntimeError("REQUIRED_CHANNEL is not configured")
    return channel


async def is_subscribed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    try:
        member = await context.bot.get_chat_member(required_channel(), user.id)
    except Exception:
        LOG.exception("Channel membership check failed")
        return False
    return member.status in {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    }


def join_markup() -> InlineKeyboardMarkup:
    channel = required_channel()
    username = channel.lstrip("@").strip()
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("加入频道", url=f"https://t.me/{username}")],
            [InlineKeyboardButton("我已关注，重新检查", callback_data="check_membership")],
        ]
    )


async def require_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await is_subscribed(update, context):
        return True
    message = update.effective_message
    if message:
        await message.reply_text(
            "请先关注指定频道，再使用解析功能。",
            reply_markup=join_markup(),
        )
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_membership(update, context):
        return
    payload = (context.args[0].lower() if context.args else "")
    quick = {
        "video": "请发送公开的抖音或小红书链接。",
        "wallet": "请发送 EVM 钱包地址，我将进行只读授权查询（绝不索要私钥）。",
        "stock": "请发送 A 股股票代码，例如 600519。结果仅供参考。",
    }
    if payload in quick:
        await update.effective_message.reply_text(
            quick[payload],
            reply_markup=ReplyKeyboardMarkup(MENU_BUTTONS, resize_keyboard=True),
        )
        return
    await update.effective_message.reply_text(
        "欢迎使用内容解析小工具。\n\n"
        "发送抖音或小红书链接即可识别。\n"
        "当前版本先完成链接识别和任务框架，媒体处理仅面向你有权使用的内容。",
        reply_markup=ReplyKeyboardMarkup(MENU_BUTTONS, resize_keyboard=True),
    )


async def check_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await is_subscribed(update, context):
        await query.answer()
        await query.edit_message_text("关注验证通过。现在可以发送抖音或小红书链接了。")
    else:
        await query.answer("还没有检测到关注，请先加入频道。", show_alert=True)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_membership(update, context):
        return
    text = (update.effective_message.text or "").strip()
    if text == "钱包授权查询":
        await update.effective_message.reply_text("钱包授权查询模块正在接入。仅支持公开链上只读查询，绝不会索要私钥或助记词。")
        return
    if text == "A股分析":
        await update.effective_message.reply_text("请发送股票代码（如 600519）。行情和分析功能正在接入，结果仅供参考，不构成投资建议。")
        return
    if text == "频道入口":
        await update.effective_message.reply_text("频道： https://t.me/jksjsjs6969")
        return
    if text == "视频解析":
        await update.effective_message.reply_text("请发送公开的抖音或小红书链接。")
        return
    link = detect_platform(text)
    if not link:
        await update.effective_message.reply_text("请发送抖音或小红书的公开链接。")
        return
    if link.platform == "其他链接":
        await update.effective_message.reply_text("暂不支持这个平台的链接。")
        return
    try:
        metadata = await fetch_public_metadata(link.url)
    except Exception:
        LOG.exception("Public metadata fetch failed")
        await update.effective_message.reply_text(
            f"已识别平台：{link.platform}\n\n链接：{link.url}\n\n"
            "暂时无法读取公开页面信息，可能是链接已失效或平台限制访问。"
        )
        return
    lines = [f"已识别平台：{link.platform}", f"链接：{link.url}"]
    if metadata.title:
        lines.append(f"标题：{metadata.title[:200]}")
    if metadata.description:
        lines.append(f"简介：{metadata.description[:300]}")
    if metadata.image:
        lines.append(f"封面：{metadata.image}")
    lines.append("\n当前仅读取公开元数据；媒体处理适配器仅面向你自己拥有或获授权的内容。")
    await update.effective_message.reply_text("\n".join(lines))


def build_application() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    required_channel()  # fail fast before polling starts
    app = Application.builder().token(token).build()
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start, filters=private))
    app.add_handler(CallbackQueryHandler(check_membership, pattern="^check_membership$"))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, handle_text))
    return app


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    # Render Web Services require a listening port. The bot still uses
    # long-polling; this tiny health endpoint keeps the service deployable.
    port = int(os.getenv("PORT", "8080"))

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: object) -> None:
            return

    health_server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=health_server.serve_forever, daemon=True).start()
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
