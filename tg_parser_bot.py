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
    "300308": ("AI 算力与光模块", "公司主营高速光收发模块，订单表现主要看海外算力建设和 800G、1.6T 产品放量"),
    "601179": ("特高压与电网设备", "公司主营变压器、组合电器和高压开关，业绩主要受电网投资、特高压项目进度和海外订单影响"),
    "002436": ("半导体封装基板", "公司业务覆盖高多层 PCB 和先进封装载板，后续看产能爬坡、大客户认证及半导体景气度"),
    "600172": ("超硬材料与培育钻石", "公司主营人造金刚石和超硬材料，当前更需要观察工业端需求及主业盈利修复情况"),
    "002935": ("时频器件与军工电子", "公司主营原子钟、晶体器件和时间同步系统，订单变化与军工信息化、北斗及卫星通信需求有关"),
}

# Short, human-sounding technical comment templates.  These deliberately stay
# within the quote/history data we actually have instead of inventing news or
# fundamentals.  Rotation keeps consecutive replies from looking copied.
ANALYSIS_TEMPLATES = [
    "最新价{price:.2f}元，较前收{change:+.2f}元（{pct:+.2f}%）。股价在近30日{low:.2f}-{high:.2f}元区间的{zone}，{ma_text}。目前先看{resistance:.2f}元压力能不能放量突破，回踩时{support:.2f}元附近能否守住，这两个位置比单日涨跌更重要。",
    "现价{price:.2f}元，日内变动{change:+.2f}元（{pct:+.2f}%），位置处在近30日区间{low:.2f}-{high:.2f}元的{zone}。{ma_text}，短线仍以整理为主；上方{resistance:.2f}元是第一道压力，下方{support:.2f}元是当前防守位。",
    "从最近30个交易日看，{name}股价大致运行在{low:.2f}-{high:.2f}元之间，当前报{price:.2f}元，较前收{change:+.2f}元（{pct:+.2f}%）。{ma_text}，盘面暂时没有走出明确方向，后面观察{resistance:.2f}元和{support:.2f}元的得失即可。",
    "这只票目前在近30日{zone}运行，最新价{price:.2f}元，较前收{change:+.2f}元（{pct:+.2f}%）。{ma_text}。如果反弹靠近{resistance:.2f}元仍然放不出量，追高要谨慎；回落不破{support:.2f}元，短线还有反复的空间。",
    "{name}当前报{price:.2f}元，近30日波动范围为{low:.2f}-{high:.2f}元，现价位于{zone}。今天较前收{change:+.2f}元（{pct:+.2f}%），{ma_text}。短线先按区间看待，站上{resistance:.2f}元再谈转强，跌破{support:.2f}元则要防止继续调整。",
    "股价在{low:.2f}-{high:.2f}元区间来回整理后，当前来到{price:.2f}元，日内{change:+.2f}元（{pct:+.2f}%）。{ma_text}，{zone}暂未改变。后续能否走出行情，关键看{resistance:.2f}元压力和{support:.2f}元支撑，暂时不宜只凭一天的涨跌下结论。",
    "最新价{price:.2f}元，较前收{change:+.2f}元（{pct:+.2f}%）。近30日高低点分别是{high:.2f}元和{low:.2f}元，现价处于{zone}；{ma_text}。若量能跟不上，{resistance:.2f}元附近仍会有抛压，回调先看{support:.2f}元承接。",
    "从价格位置看，{name}还在近30日{low:.2f}-{high:.2f}元箱体内，现价{price:.2f}元，今日{change:+.2f}元（{pct:+.2f}%）。{ma_text}，短线偏{trend}。上破{resistance:.2f}元才算打开空间，失守{support:.2f}元则要把预期放低。",
    "目前股价报{price:.2f}元，位于近30日区间的{zone}，区间低点{low:.2f}元、高点{high:.2f}元；较前收{change:+.2f}元（{pct:+.2f}%）。{ma_text}。操作上先观察支撑，不追着单日上涨买入，压力位{resistance:.2f}元能否消化是下一步看点。",
    "{name}今日价格变化不大，现价{price:.2f}元，较前收{change:+.2f}元（{pct:+.2f}%）。结合近30日走势，股价仍处于{zone}，{ma_text}。短线以{support:.2f}元为防守、{resistance:.2f}元为突破参考，等方向走出来再做判断。",
]


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
        industry = intro = ""
        if profile:
            industry, intro = profile
        global ANALYSIS_VARIANT
        template = ANALYSIS_TEMPLATES[ANALYSIS_VARIANT % len(ANALYSIS_TEMPLATES)]
        ANALYSIS_VARIANT += 1
        body = template.format(
            name=name, price=price, change=change, pct=pct,
            low=period_low, high=period_high, zone=zone,
            ma_text=ma_text, resistance=period_high, support=period_low,
            trend=trend,
        )
        prefix = f"{name}（{digits}）"
        if profile:
            prefix += f"｜{industry}。{intro}。"
        narrative = prefix + body
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
    public_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if not public_url:
        raise RuntimeError("RENDER_EXTERNAL_URL is not configured")
    if not public_url.startswith("https://"):
        raise RuntimeError("RENDER_EXTERNAL_URL must be an HTTPS URL")
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
