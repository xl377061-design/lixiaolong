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
import asyncio
import tempfile
from pathlib import Path
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
STOCK_CODE_RE = re.compile(r"^(?:sh|sz)?(\d{6})$", re.IGNORECASE)


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


def fetch_stock_quote(code: str) -> str:
    """Read a public Tencent quote; no trading or account access."""
    match = STOCK_CODE_RE.fullmatch(code.strip())
    if not match:
        raise ValueError("股票代码应为 6 位数字")
    digits = match.group(1)
    market = "sh" if digits.startswith(("6", "68")) else "sz"
    symbol = market + digits
    req = Request(f"https://qt.gtimg.cn/q={symbol}", headers={"User-Agent": "Mozilla/5.0"})
    raw = urlopen(req, timeout=10).read().decode("gbk", errors="replace")
    payload = raw.split('="', 1)[-1].rsplit('"', 1)[0]
    fields = payload.split("~")
    if len(fields) < 6 or not fields[1]:
        raise RuntimeError("未找到该股票行情")
    name, price, previous = fields[1], float(fields[3]), float(fields[4])
    change = price - previous
    pct = (change / previous * 100) if previous else 0.0
    return f"{name}（{digits}）\n现价：{price:.2f}\n涨跌：{change:+.2f}（{pct:+.2f}%）\n\n数据来自公开行情，仅供参考，不构成投资建议。"


def make_stock_chart(code: str) -> tuple[str, Path]:
    """Create a simple 30-day closing-price chart from a public quote feed."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    match = STOCK_CODE_RE.fullmatch(code.strip())
    if not match:
        raise ValueError("股票代码应为 6 位数字")
    digits = match.group(1)
    market = "sh" if digits.startswith(("6", "68")) else "sz"
    symbol = market + digits
    req = Request(
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,30,qfq",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    data = json.loads(urlopen(req, timeout=10).read().decode("utf-8", errors="replace"))
    rows = data.get("data", {}).get(symbol, {}).get("qfqday", [])
    if len(rows) < 5:
        raise RuntimeError("历史行情不足")
    dates = [r[0][5:] for r in rows]
    closes = [float(r[2]) for r in rows]
    ma5 = [sum(closes[max(0, i-4):i+1]) / min(i + 1, 5) for i in range(len(closes))]
    ma20 = [sum(closes[max(0, i-19):i+1]) / min(i + 1, 20) for i in range(len(closes))]
    path = Path(tempfile.gettempdir()) / f"stock-{digits}.png"
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=140)
    ax.plot(dates, closes, label="收盘", color="#2563eb", linewidth=2)
    ax.plot(dates, ma5, label="MA5", color="#f59e0b", linewidth=1.3)
    ax.plot(dates, ma20, label="MA20", color="#16a34a", linewidth=1.3)
    ax.set_title(f"{digits} 近30日行情")
    ax.grid(alpha=0.2)
    ax.legend()
    ax.tick_params(axis="x", rotation=45, labelsize=8)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return digits, path


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
    await update.effective_message.reply_text(
        "欢迎使用 A 股个股解析。\n\n"
        "请发送 6 位股票代码，例如 600519。\n"
        "行情数据仅供参考，不构成投资建议。",
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
    if STOCK_CODE_RE.fullmatch(text):
        try:
            quote = await asyncio.to_thread(fetch_stock_quote, text)
            digits, chart = await asyncio.to_thread(make_stock_chart, text)
            with chart.open("rb") as image:
                await update.effective_message.reply_photo(photo=image, caption=quote)
            chart.unlink(missing_ok=True)
        except Exception:
            await update.effective_message.reply_text("暂时无法读取该股票行情，请检查代码或稍后再试。")
        return
    await update.effective_message.reply_text("请发送 6 位 A 股股票代码，例如 600519。")


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
