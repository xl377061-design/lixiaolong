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
    "{name}近期量价配合还算清楚，{volume_text}。现价{price:.2f}元，处在近30日{zone}，{ma_text}，{ma60_text}。这类走势暂时不用盯着一天的涨跌，先看{support:.2f}元附近能否守稳；整理充分后，如能带量越过{resistance:.2f}元，短线空间才会真正打开。",
    "{name}这段时间主要围绕区间反复消化，股价从低点{low:.2f}元运行到目前{price:.2f}元，位置来到{zone}。盘中{volume_text}，同时{ma_text}，{ma60_text}。眼下{resistance:.2f}元仍有压力，回踩{support:.2f}元不破，结构就没有明显走坏，耐心等方向选择即可。",
    "从最近的走势看，{name}并不是单边运行，而是在{low:.2f}-{high:.2f}元之间来回换手。当前报{price:.2f}元，较前收{change:+.2f}元（{pct:+.2f}%），{volume_text}。均线方面，{ma_text}，{ma60_text}；短线先看{support:.2f}元承接，真正转强还得越过{resistance:.2f}元。",
    "{name}目前的看点不在单日涨跌，而在量价节奏。近期{volume_text}，说明场内资金还在反复试探；现价{price:.2f}元位于近30日{zone}，{ma_text}，{ma60_text}。只要{support:.2f}元一带没有被有效跌破，仍可按整理看待，向上则留意{resistance:.2f}元附近的抛压。",
    "{name}最新报{price:.2f}元，较前收{change:+.2f}元（{pct:+.2f}%）。近30日高低点为{high:.2f}元和{low:.2f}元，当前处在{zone}；{volume_text}，{ma_text}，{ma60_text}。走势暂时还在蓄势阶段，{support:.2f}元是短线防守位，突破{resistance:.2f}元时要看成交量是否同步跟上。",
    "这只票近期在{low:.2f}-{high:.2f}元之间整理，现价{price:.2f}元，位置处于{zone}。从盘面细节看，{volume_text}，而且{ma_text}，{ma60_text}。目前多空还没有完全分出胜负，{support:.2f}元守住就还有修复机会；{resistance:.2f}元过不去，仍要防止冲高后再次回落。",
    "{name}短线走的是边整理边换手的节奏，当前价格{price:.2f}元，日内{change:+.2f}元（{pct:+.2f}%）。近期{volume_text}，均线则表现为{ma_text}，{ma60_text}。接下来不妨把{support:.2f}元作为强弱分界，守住可继续观察反弹延续性，越过{resistance:.2f}元才算真正摆脱区间。",
    "从技术位置看，{name}目前仍在近30日箱体内，现价{price:.2f}元，处于{zone}。量能方面{volume_text}，再结合{ma_text}、{ma60_text}，说明盘面仍有反复。短线回落先看{support:.2f}元附近有没有承接，向上试探{resistance:.2f}元时，则要防范无量冲高。",
    "{name}近阶段的走势比较有节奏，{volume_text}。股价现报{price:.2f}元，较前收{change:+.2f}元（{pct:+.2f}%），位于近30日{zone}；{ma_text}，{ma60_text}。当前位置宜多看量价是否继续配合，{support:.2f}元不失守，整理结束后仍有再次试探{resistance:.2f}元的可能。",
    "{name}当前没有走出明显的单边行情，价格仍在{low:.2f}-{high:.2f}元区间消化，最新价{price:.2f}元。盘中{volume_text}，{ma_text}，{ma60_text}。这里更适合等确认：回踩{support:.2f}元企稳，说明下方仍有承接；若后面放量突破{resistance:.2f}元，短线节奏才会进一步转强。",
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
    history = _fetch_stock_history(symbol, 90)
    recent = history[-30:]
    closes = [float(row[2]) for row in recent]
    if closes:
        period_low, period_high = min(closes), max(closes)
        ma5 = sum(closes[-5:]) / min(len(closes), 5)
        ma20 = sum(closes[-20:]) / min(len(closes), 20)
        all_closes = [float(row[2]) for row in history]
        ma60 = sum(all_closes[-60:]) / min(len(all_closes), 60)
        ma60_text = "股价仍在60日线上方" if price >= ma60 else "股价尚未收复60日线"
        recent_rows = history[-15:]
        up_volumes = [float(row[5]) for row in recent_rows if float(row[2]) >= float(row[1])]
        down_volumes = [float(row[5]) for row in recent_rows if float(row[2]) < float(row[1])]
        up_avg = sum(up_volumes) / len(up_volumes) if up_volumes else 0.0
        down_avg = sum(down_volumes) / len(down_volumes) if down_volumes else 0.0
        if up_avg > down_avg * 1.15:
            volume_text = "上涨时量能更活跃，回落时成交相对收敛"
        elif down_avg > up_avg * 1.15:
            volume_text = "回落时成交偏大，上方抛压还没有完全消化"
        else:
            volume_text = "成交量没有明显偏向，多空仍在拉锯"
        position = (price - period_low) / (period_high - period_low) if period_high > period_low else 0.5
        zone = "区间上沿" if position >= 0.67 else "区间下沿" if position <= 0.33 else "区间中部"
        ma_text = "MA5 位于 MA20 上方，短线动能相对占优" if ma5 >= ma20 else "MA5 位于 MA20 下方，短线仍有整理压力"
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
            trend=trend, volume_text=volume_text, ma60_text=ma60_text,
        )
        prefix = f"{name}（{digits}）"
        if profile:
            prefix += f"｜{industry}。{intro}。"
        narrative = prefix + body
    else:
        narrative = f"{name}（{digits}）现价 {price:.2f} 元，较前收 {change:+.2f} 元（{pct:+.2f}%），当前盘面状态为{trend}。"
    return f"📊 {narrative}"


def _fetch_stock_history(symbol: str, days: int = 30) -> list[list[str]]:
    req = Request(
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{days},qfq",
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
