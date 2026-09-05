"""Telegram content parser bot MVP.

This first version intentionally keeps platform parsing behind a small adapter
interface. It verifies channel membership and classifies public links, but does
not collect login cookies or bypass platform access controls.
"""

from __future__ import annotations

import logging
import os
import re
import asyncio
import io
import tempfile
from pathlib import Path
import json
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ChatMemberStatus, ParseMode
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
ANALYSIS_VARIANT = 0
STOCK_PROFILES = {
    "300308": ("AI 算力与光模块龙头", "中际旭创作为全球光模块核心厂商，主营高速光收发模块，业务与全球算力基础设施及 AI 资本开支周期高度相关"),
    "601179": ("特高压与电网设备龙头", "中国西电作为特高压及输配电设备核心厂商，主营变压器、组合电器及高压开关，业务与电网建设和电力扩容需求密切相关"),
    "002436": ("半导体封装基板龙头", "兴森科技作为高端 PCB 及半导体封装基板厂商，主营高多层板与先进封装载板，业务与国产算力芯片封测及半导体周期相关"),
    "600172": ("超硬材料与培育钻石龙头", "黄河旋风主营人造金刚石、超硬材料及培育钻石，业务覆盖工业精密加工、半导体散热及消费端培育钻石需求"),
    "002935": ("时频器件与军工电子龙头", "天奥电子主营原子钟、晶体器件及时间同步系统，业务与国防信息化、北斗导航及卫星互联网建设相关"),
}


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
    trend = "偏强" if pct >= 1 else "偏弱" if pct <= -1 else "震荡"

    # Use the same public history feed as the chart to produce a readable,
    # deterministic narrative.  This deliberately does not invent company
    # fundamentals or call an AI model.
    history = _fetch_stock_history(symbol)
    closes = [float(row[2]) for row in history]
    if closes:
        period_low, period_high = min(closes), max(closes)
        ma5 = sum(closes[-5:]) / min(len(closes), 5)
        ma20 = sum(closes[-20:]) / min(len(closes), 20)
        position = (price - period_low) / (period_high - period_low) if period_high > period_low else 0.5
        zone = "区间上沿" if position >= 0.67 else "区间下沿" if position <= 0.33 else "区间中部"
        ma_text = "MA5 位于 MA20 上方，短线动能相对占优" if ma5 >= ma20 else "MA5 位于 MA20 下方，短线仍有整理压力"
        outlook = (
            f"后市重点观察能否放量突破 {period_high:.2f} 附近压力并延续强势；"
            f"若回落跌破 {period_low:.2f} 附近支撑，则需留意趋势转弱。"
        )
        profile = STOCK_PROFILES.get(digits)
        if profile:
            industry, intro = profile
            narrative = (
                f"{name}（{digits}）｜{industry}。{intro}。"
                f"从当前盘面看，股价处于近30日{zone}，现价 {price:.2f} 元，较前收 {change:+.2f} 元（{pct:+.2f}%）。"
                f"近30个交易日运行区间约为 {period_low:.2f}-{period_high:.2f} 元，{ma_text}，整体呈{trend}特征。"
                f"短期需关注前期套牢筹码的消化和成交量配合，{outlook}"
            )
        else:
            templates = [
            (f"{name}（{digits}）目前运行在近30日{zone}，现价 {price:.2f} 元，较前收 {change:+.2f} 元（{pct:+.2f}%）。"
             f"近30个交易日价格区间约为 {period_low:.2f}-{period_high:.2f} 元，{ma_text}，盘面整体呈{trend}特征。{outlook}"),
            (f"从技术面看，{name}（{digits}）现价 {price:.2f} 元，日内变动 {change:+.2f} 元（{pct:+.2f}%），"
             f"股价位于近30日 {period_low:.2f}-{period_high:.2f} 元波动区间的{zone}。目前{ma_text}，短线节奏偏{trend}，"
             f"后续应重点观察 {period_high:.2f} 压力与 {period_low:.2f} 支撑的得失。"),
            (f"{name}（{digits}）最新报价 {price:.2f} 元，较前收{change:+.2f} 元，涨跌幅 {pct:+.2f}%。"
             f"结合近30日走势，价格高低点约为 {period_high:.2f}/{period_low:.2f} 元，当前处于{zone}；{ma_text}。"
             f"若后续放量站稳 {period_high:.2f} 元上方，趋势有望延续；反之跌破 {period_low:.2f} 元需防范转弱。"),
            (f"{name}（{digits}）当前处于{trend}状态，现价 {price:.2f} 元，单日涨跌 {change:+.2f} 元（{pct:+.2f}%）。"
             f"近30个交易日运行范围为 {period_low:.2f}-{period_high:.2f} 元，现价位于{zone}，{ma_text}。"
             f"短线不宜只看单日涨跌，后续关键在于支撑 {period_low:.2f} 元能否守住，以及能否有效消化 {period_high:.2f} 元附近压力。"),
            ]
            # Rotate through the templates so consecutive requests never appear
            # to use the same wording (random choice could repeat by chance).
            global ANALYSIS_VARIANT
            narrative = templates[ANALYSIS_VARIANT % len(templates)]
            ANALYSIS_VARIANT += 1
    else:
        narrative = f"{name}（{digits}）现价 {price:.2f} 元，较前收 {change:+.2f} 元（{pct:+.2f}%），当前盘面状态为{trend}。"
    return f"📊 {narrative}"


def _fetch_stock_history(symbol: str) -> list[list[str]]:
    req = Request(
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,30,qfq",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    data = json.loads(urlopen(req, timeout=10).read().decode("utf-8", errors="replace"))
    rows = data.get("data", {}).get(symbol, {}).get("qfqday", [])
    return rows


def make_stock_chart(code: str) -> tuple[str, Path]:
    """Create a simple 30-day closing-price chart from a public quote feed."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    match = STOCK_CODE_RE.fullmatch(code.strip())
    if not match:
        raise ValueError("股票代码应为 6 位数字")
    digits = match.group(1)
    market = "sh" if digits.startswith(("6", "68")) else "sz"
    symbol = market + digits
    rows = _fetch_stock_history(symbol)
    if len(rows) < 5:
        raise RuntimeError("历史行情不足")
    dates = [r[0][5:] for r in rows]
    opens = [float(r[1]) for r in rows]
    closes = [float(r[2]) for r in rows]
    highs = [float(r[3]) for r in rows]
    lows = [float(r[4]) for r in rows]
    volumes = [float(r[5]) for r in rows]
    ma5 = [sum(closes[max(0, i-4):i+1]) / min(i + 1, 5) for i in range(len(closes))]
    ma20 = [sum(closes[max(0, i-19):i+1]) / min(i + 1, 20) for i in range(len(closes))]
    path = Path(tempfile.gettempdir()) / f"stock-{digits}.png"
    # Prefer common CJK fonts when available so chart labels stay Chinese on
    # both Render Linux and local Windows environments.
    plt.rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "WenQuanYi Zen Hei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig = plt.figure(figsize=(8.8, 5.9), dpi=140)
    grid = fig.add_gridspec(2, 2, width_ratios=[3.5, 1.15], height_ratios=[3, 1],
                            wspace=0.08, hspace=0.08)
    ax = fig.add_subplot(grid[0, 0])
    vol_ax = fig.add_subplot(grid[1, 0], sharex=ax)
    chip_ax = fig.add_subplot(grid[:, 1], sharey=ax)
    fig.patch.set_facecolor("#f8fafc")
    ax.set_facecolor("#f8fafc")
    for i, (op, cl, hi, lo) in enumerate(zip(opens, closes, highs, lows)):
        color = "#dc2626" if cl >= op else "#16a34a"
        ax.vlines(i, lo, hi, color=color, linewidth=1)
        ax.add_patch(Rectangle((i - 0.32, min(op, cl)), 0.64, max(abs(cl-op), 0.01),
                               facecolor=color, edgecolor=color, alpha=0.9))
    ax.plot(range(len(closes)), ma5, label="5日均线", color="#f59e0b", linewidth=1.4)
    ax.plot(range(len(closes)), ma20, label="20日均线", color="#2563eb", linewidth=1.4)
    support, resistance = min(closes), max(closes)
    ax.axhline(resistance, color="#dc2626", linestyle="--", linewidth=1.0, label="压力位")
    ax.axhline(support, color="#16a34a", linestyle="--", linewidth=1.0, label="支撑位")
    ax.annotate(f"压力位 {resistance:.2f}", xy=(len(closes) - 1, resistance),
                xytext=(-6, 5), textcoords="offset points", ha="right", fontsize=8, color="#dc2626")
    ax.annotate(f"支撑位 {support:.2f}", xy=(len(closes) - 1, support),
                xytext=(-6, 5), textcoords="offset points", ha="right", fontsize=8, color="#16a34a")
    ax.set_title(f"{digits}｜30日K线", loc="left", fontweight="bold")
    ax.grid(alpha=0.18, linestyle="--")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, ncol=3, loc="upper left")
    vol_ax.bar(range(len(volumes)), volumes, color="#94a3b8", width=0.65)
    vol_ax.set_ylabel("成交量", fontsize=8)
    vol_ax.grid(alpha=0.12, linestyle="--")
    vol_ax.spines[["top", "right"]].set_visible(False)
    # A rule-based chip-peak proxy: distribution of the last 30 closing prices.
    # It is not a broker's proprietary cost-distribution dataset.
    chip_ax.hist(closes, bins=12, orientation="horizontal", color="#8b5cf6", alpha=0.75,
                 edgecolor="white", linewidth=0.4)
    chip_ax.axhline(support, color="#16a34a", linestyle="--", linewidth=0.9)
    chip_ax.axhline(resistance, color="#dc2626", linestyle="--", linewidth=0.9)
    chip_ax.set_title("筹码峰", fontsize=10, fontweight="bold")
    chip_ax.set_xlabel("密集度", fontsize=8)
    chip_ax.grid(axis="y", alpha=0.12, linestyle="--")
    chip_ax.spines[["top", "right"]].set_visible(False)
    chip_ax.tick_params(axis="y", labelleft=False)
    ax.set_xticks(range(0, len(dates), max(1, len(dates)//8)))
    ax.set_xticklabels([dates[i] for i in range(0, len(dates), max(1, len(dates)//8))], rotation=45, fontsize=8)
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
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 进入频道", url="https://t.me/jksjsjs6969")]
        ]),
        # Also clears any legacy reply keyboard in the user's Telegram client.
        # The removal is attached to the welcome message so no placeholder
        # "正在更新界面" message is shown.
    )


async def check_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if await is_subscribed(update, context):
        await query.answer()
        await query.edit_message_text("关注验证通过。现在可以发送 6 位 A 股股票代码了。")
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
            image_bytes = chart.read_bytes()
            await update.effective_message.reply_photo(
                photo=io.BytesIO(image_bytes),
                caption=quote,
            )
            # Republish the result to the owner's channel.  A channel failure
            # must not hide the result from the user who requested it.
            try:
                channel_image = io.BytesIO(image_bytes)
                channel_image.name = f"stock-{digits}.png"
                await context.bot.send_photo(
                    chat_id=required_channel(),
                    photo=channel_image,
                    caption=(
                        f"{quote}\n\n"
                        '<a href="https://t.me/xiaolongko_ai_bot?start=stock">'
                        "📊 个股解析，请点击进入机器人</a>"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                LOG.exception("Failed to publish stock result to channel")
            chart.unlink(missing_ok=True)
        except Exception:
            await update.effective_message.reply_text("暂时无法读取该股票行情，请检查代码或稍后再试。")
        return
    if text == "频道入口":
        await update.effective_message.reply_text("你的频道： https://t.me/jksjsjs6969")
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
    # Use Telegram Webhook instead of long polling. Telegram's inbound POST
    # wakes a sleeping Render instance and avoids duplicate getUpdates conflicts.
    port = int(os.getenv("PORT", "8080"))
    public_url = os.getenv("RENDER_EXTERNAL_URL", "https://lixiaolong-tg-parser.onrender.com").rstrip("/")
    webhook_path = os.getenv("WEBHOOK_PATH", "telegram-webhook").strip("/")
    # Telegram accepts 1-256 characters for this secret. Deriving a stable
    # default avoids adding another Render secret while still authenticating
    # webhook requests; WEBHOOK_SECRET can override it when desired.
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    import hashlib
    webhook_secret = os.getenv("WEBHOOK_SECRET", hashlib.sha256(token.encode()).hexdigest()[:32])
    build_application().run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=f"{public_url}/{webhook_path}",
        secret_token=webhook_secret,
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
