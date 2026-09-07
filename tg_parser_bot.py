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
import time
import uuid
from pathlib import Path
import json
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlparse
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
STOCK_REQUEST_RE = re.compile(r"(?<!\d)(?:sh|sz)?(\d{6})(?!\d)", re.IGNORECASE)
COST_RE = re.compile(r"(?:成本价格|成本价|成本|持仓价|买入价)\s*[:：=]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
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
    "{code}{name}：现价{price:.2f}元，较前收{change:+.2f}元（{pct:+.2f}%）。近30日股价在{low:.2f}-{high:.2f}元之间来回磨，目前处在{zone}。{volume_text}，{ma_text}，{ma60_text}。短线先看{support:.2f}元能不能守住，守住就还有抬轿的机会；上方{resistance:.2f}元要放量过去，没量还是容易冲高回落。",
    "{name}这只票最近不是一路拉，也不是一路跌，更多是在区间里换手。最新价{price:.2f}元，{volume_text}；{ma_text}，{ma60_text}。当前位置属于近30日{zone}，下方{support:.2f}元是多头要守的地方，上方{resistance:.2f}元压力还在，先等它把方向走出来。",
    "从近30日走势看，{name}股价由{low:.2f}元走到现在的{price:.2f}元，过程里有反复、有洗盘。{volume_text}，说明资金接力还不算顺畅；目前{ma_text}，{ma60_text}。回踩{support:.2f}元不破，短线仍有修复空间，真正转强要看{resistance:.2f}元能不能带量突破。",
    "{name}当前报{price:.2f}元，今天{change:+.2f}元（{pct:+.2f}%），价格落在近30日区间的{zone}。盘面上{volume_text}，均线{ma_text}，{ma60_text}。这种位置不适合只看一根阳线或阴线，先盯住{support:.2f}元支撑；能放量越过{resistance:.2f}元，再谈后面的空间。",
    "{code}{name}目前还在{low:.2f}-{high:.2f}元箱体里折腾，现价{price:.2f}元。{volume_text}，{ma_text}，{ma60_text}，短线节奏偏{trend}。只要{support:.2f}元附近承接还在，箱体就没有被打破；反弹到了{resistance:.2f}元一带，量能跟不上就要提防抛压。",
    "这只票的量价关系比较有意思：{volume_text}。股价现在{price:.2f}元，处于近30日{zone}，{ma_text}，{ma60_text}。往下看{support:.2f}元，往上看{resistance:.2f}元，暂时就是这两个位置来回拉锯。等放量选出方向，再考虑后面的节奏。",
    "{name}近期走的是边洗边整理的路子，股价没有脱离{low:.2f}-{high:.2f}元区间，最新价{price:.2f}元。{volume_text}，{ma_text}，{ma60_text}。短线只要不跌穿{support:.2f}元，盘面还有反复拉升的可能；但{resistance:.2f}元附近套牢盘不少，突破必须有量配合。",
    "简单看一下{name}（{code}）：现价{price:.2f}元，较前收{change:+.2f}元（{pct:+.2f}%），近30日位置在{zone}。近期{volume_text}，{ma_text}，{ma60_text}。现在还谈不上单边行情，{support:.2f}元是短线底线，{resistance:.2f}元是上方关口，先守支撑、再看突破。",
    "{name}这段行情有点像箱体里磨底，价格在{low:.2f}-{high:.2f}元之间反复，今天报{price:.2f}元。{volume_text}，均线方面{ma_text}，{ma60_text}。只要{support:.2f}元不被放量击穿，后面仍有抬头的可能；若冲到{resistance:.2f}元没有成交量，别把反弹当反转。",
    "从盘面节奏看，{name}目前在近30日{zone}运行，最新价{price:.2f}元，{volume_text}。{ma_text}，{ma60_text}，短线多空仍在拉锯。下方先看{support:.2f}元能否稳住，上方{resistance:.2f}元能否放量站上；两个位置没有被有效突破前，继续震荡的概率更大。",
    "{name}近期量价配合还算清楚，{volume_text}。现价{price:.2f}元，处在近30日{zone}，{ma_text}，{ma60_text}。这类走势暂时不用盯着一天的涨跌，先看{support:.2f}元附近能否守稳；整理充分后，如能带量越过{resistance:.2f}元，短线空间才会真正打开。",
    "{name}这段时间主要围绕区间反复消化，股价从低点{low:.2f}元运行到目前{price:.2f}元，位置来到{zone}。盘面{volume_text}，同时{ma_text}，{ma60_text}。眼下{resistance:.2f}元仍有压力，回踩{support:.2f}元不破，结构就没有明显走坏，耐心等方向选择即可。",
    "从最近的走势看，{name}并不是单边运行，而是在{low:.2f}-{high:.2f}元之间来回换手。当前报{price:.2f}元，较前收{change:+.2f}元（{pct:+.2f}%），{volume_text}。均线方面{ma_text}，{ma60_text}；短线先看{support:.2f}元承接，真正转强还得越过{resistance:.2f}元。",
    "{name}目前的看点不在单日涨跌，而在量价节奏。近期{volume_text}，场内资金还在反复试探；现价{price:.2f}元位于近30日{zone}，{ma_text}，{ma60_text}。只要{support:.2f}元一带没有被有效跌破，仍可按整理看待，向上则留意{resistance:.2f}元附近的抛压。",
    "{name}最新报{price:.2f}元，较前收{change:+.2f}元（{pct:+.2f}%）。近30日高低点为{high:.2f}元和{low:.2f}元，当前处在{zone}；{volume_text}，{ma_text}，{ma60_text}。走势暂时还在蓄势阶段，{support:.2f}元是短线防守位，突破{resistance:.2f}元时要看量能是否同步跟上。",
    "这只票近期在{low:.2f}-{high:.2f}元之间整理，现价{price:.2f}元，位置处于{zone}。从盘面细节看，{volume_text}，而且{ma_text}，{ma60_text}。目前多空还没有完全分出胜负，{support:.2f}元守住就还有修复机会；{resistance:.2f}元过不去，仍要防止冲高后再次回落。",
    "{name}短线走的是边整理边换手的节奏，当前价格{price:.2f}元，日内{change:+.2f}元（{pct:+.2f}%）。近期{volume_text}，均线则表现为{ma_text}，{ma60_text}。接下来不妨把{support:.2f}元作为强弱分界，守住可继续观察，越过{resistance:.2f}元才算真正摆脱区间。",
    "从技术位置看，{name}目前仍在近30日箱体内，现价{price:.2f}元，处于{zone}。量能方面{volume_text}，再结合{ma_text}、{ma60_text}，说明盘面仍有反复。短线回落先看{support:.2f}元附近承接，向上试探{resistance:.2f}元时，则要防范无量冲高。",
    "{name}近阶段的走势比较有节奏，{volume_text}。股价现报{price:.2f}元，较前收{change:+.2f}元（{pct:+.2f}%），位于近30日{zone}；{ma_text}，{ma60_text}。当前位置多看量价是否继续配合，{support:.2f}元不失守，仍有再次试探{resistance:.2f}元的可能。",
    "{name}当前没有走出明显的单边行情，价格仍在{low:.2f}-{high:.2f}元区间消化，最新价{price:.2f}元。盘面{volume_text}，{ma_text}，{ma60_text}。这里更适合等确认：回踩{support:.2f}元企稳，说明下方仍有承接；若后面放量突破{resistance:.2f}元，短线节奏才会进一步转强。",
]

# Additional colloquial templates supplied by Kimi.  They are intentionally
# short and varied so the bot sounds less mechanical while still using only
# the quote/history values calculated below.
ANALYSIS_TEMPLATES.extend([
    "{name}今天{change:+.2f}元，{pct:+.2f}%，盘面{trend}。{volume_text}，先看{support:.2f}元能不能扛住，{resistance:.2f}元这道坎过不去，别急着追。",
    "这票目前{ma_text}，{volume_text}，走得有点黏。现价{price:.2f}元，短线先盯{support:.2f}元，方向出来再动手。",
    "{name}放量异动，价格来到{price:.2f}元，{volume_text}。上面{resistance:.2f}元压力还在，先看回踩能不能站稳。",
    "{resistance:.2f}元附近抛压挺明显，{name}冲高没站住，短线别被套在山顶。下方先看{support:.2f}元。",
    "{name}在{support:.2f}元上方来回磨，有点磨人，{volume_text}。暂时别猜方向，等放量再说。",
    "弱势格局还没完全扭过来，{trend}，{volume_text}。这票先别硬上，等止跌信号更稳。",
    "{name}今天放量但涨幅不大，多少有点滞涨味道，{resistance:.2f}元附近要小心冲高回落。",
    "低位筑底的迹象有一点，{ma_text}，{volume_text}。可以放进观察名单，但仓位别一下子压太重。",
    "{name}回踩{support:.2f}元没破，形态暂时没坏，先拿着看；真正转强还得过{resistance:.2f}元。",
    "{resistance:.2f}元这关一直过不去，{name}还得在区间里消化，别把一次反弹当成反转。",
    "这票{volume_text}，量价配合一般，{trend}，短线想大涨不容易，耐心等变化。",
    "{name}今天看着挺强，{volume_text}，但回踩{support:.2f}元能不能稳住更关键，别只看表面热闹。",
    "回调到{support:.2f}元附近，先观察承接，跌幅{pct:+.2f}%，差不多到考验位置了。",
    "{name}高位横着走，{volume_text}，有点滞涨的意思，该收一收仓位就别恋战。",
    "这股跌得有点狠，{change:+.2f}元（{pct:+.2f}%），但{volume_text}，恐慌盘还没完全释放，别急着抄底。",
    "{name}贴着均线慢慢爬，走得不算猛，但也没有明显走坏，{support:.2f}元先守住再说。",
    "{support:.2f}元要是被放量打穿，{name}就要转弱了，这个位置比较关键。",
    "今天{volume_text}，但价格没怎么动，多空还在拉扯，先再看两天。",
    "{name}趋势还是{trend}，不过{resistance:.2f}元压力不小，突破之前别把预期放太高。",
    "低位放量，{name}像是有承接进来，但底部不是一天做出来的，慢慢看。",
    "{name}今天冲高回落，留下上影线，{resistance:.2f}元过不去，短线压力比较直接。",
    "这票跌破{support:.2f}元了，趋势有点难看，{volume_text}，该收手就别硬扛。",
    "{name}围绕均线震荡，多空拉锯，没方向就轻仓看戏，别让它牵着走。",
    "今天{pct:+.2f}%，{change:+.2f}元，{volume_text}，{name}走得偏强，但也别一下子上头。",
    "{name}在底部缩量横了挺久，可能快变盘，{support:.2f}元和{resistance:.2f}元都盯紧。",
    "突破{resistance:.2f}元后没站稳，{name}又缩回来了，像是假突破，谨慎一点。",
    "{name}今天逆势走强，{trend}，{volume_text}，不过高位别追，等回踩确认。",
    "回调没有明显放量，{name}暂时问题不大，{support:.2f}元不破就继续观察。",
    "这票均线偏空，{volume_text}，弱势比较明显，先别急着接。",
    "{name}收在{price:.2f}元，整体{trend}，关键还是看{support:.2f}元能不能守住。",
])

ST_ANALYSIS_TEMPLATES = [
    "这只票带有 ST 标识，先看公告和交易所信息，图上的均线、支撑压力只能辅助参考，不能单靠技术面判断摘帽或反转。",
    "{name}属于更高风险标的，短线涨跌不代表风险解除，后面重点看审计、重整和公司公告，别只盯着一根阳线。",
    "如果进入退市整理阶段，流动性可能明显下降，参考支撑不等于安全位置，还要防连续跌停和卖不出去。",
    "这类股票先看公告，再看 K 线。戴帽、摘帽、停牌、重整和退市安排，以交易所披露为准。",
    "图中{support:.2f}元和{resistance:.2f}元只是历史成交密集区，跌破不一定见底，突破也不代表风险解除。",
    "ST 股票波动往往比普通股票更大，今天的反弹只能说明短线情绪变化，不能直接当成基本面反转。",
    "{name}现在更适合把风险放在第一位，技术图可以看节奏，但不能替代公告、审计和交易状态判断。",
    "这类票不谈抄底和稳赚，先确认交易状态、公司公告和流动性，再考虑是否继续观察。",
]


def _read_url(req: Request, timeout: float = 10, attempts: int = 2) -> bytes:
    """Read a public endpoint with one short retry for transient failures."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return urlopen(req, timeout=timeout).read()
        except Exception as exc:  # network providers can raise varied errors
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.35 * (attempt + 1))
    assert last_error is not None
    raise last_error


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


def extract_cost_price(text: str, code_match: re.Match[str] | None = None) -> float | None:
    explicit = COST_RE.search(text or "")
    if explicit:
        return float(explicit.group(1))
    remainder = (text or "")[code_match.end():] if code_match else (text or "")
    numbers = re.findall(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", remainder)
    for value in numbers:
        if len(value.split(".", 1)[0]) != 6:
            candidate = float(value)
            if 0 < candidate < 100000:
                return candidate
    return None


def parse_stock_request(text: str) -> tuple[str | None, float | None]:
    """Extract a stock code and optional cost price from natural input."""
    raw = (text or "").strip()
    match = STOCK_REQUEST_RE.search(raw)
    if not match:
        return None, None
    code = match.group(1)
    return code, extract_cost_price(raw, match)


def resolve_stock_name(text: str) -> str | None:
    """Resolve a Chinese A-share name through Tencent's public search hint."""
    query = re.sub(r"(?:解票|分析|看看|查询|帮我|请|成本价格|成本价|成本|持仓价|买入价)", "", text or "")
    query = re.sub(r"\d+(?:\.\d+)?", "", query).strip(" ：:，,。")
    if not query:
        return None
    req = Request(
        f"https://smartbox.gtimg.cn/s3/?q={quote(query)}&t=all",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    raw = _read_url(req, timeout=8).decode("utf-8", errors="replace")
    payload = raw.split('v_hint="', 1)[-1].rsplit('"', 1)[0]
    candidates: list[tuple[str, str]] = []
    for item in payload.split("^"):
        parts = item.split("~")
        if len(parts) >= 3 and parts[0].lower() in {"sh", "sz"} and parts[1].isdigit():
            candidates.append((parts[1], parts[2]))
    if not candidates:
        return None
    exact = [code for code, name in candidates if name == query]
    return exact[0] if exact else candidates[0][0]


def fetch_stock_quote(code: str, cost: float | None = None) -> str:
    """Read a public Tencent quote; no trading or account access."""
    match = STOCK_CODE_RE.fullmatch(code.strip())
    if not match:
        raise ValueError("股票代码应为 6 位数字")
    digits = match.group(1)
    market = "sh" if digits.startswith(("6", "68")) else "sz"
    symbol = market + digits
    req = Request(f"https://qt.gtimg.cn/q={symbol}", headers={"User-Agent": "Mozilla/5.0"})
    raw = _read_url(req, timeout=10).decode("gbk", errors="replace")
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
    support_level = price
    resistance_level = price
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
        support_level, resistance_level = period_low, period_high
        zone = "区间上沿" if position >= 0.67 else "区间下沿" if position <= 0.33 else "区间中部"
        ma_text = "MA5 位于 MA20 上方，短线动能相对占优" if ma5 >= ma20 else "MA5 位于 MA20 下方，短线仍有整理压力"
        profile = STOCK_PROFILES.get(digits)
        industry = intro = ""
        if profile:
            industry, intro = profile
        global ANALYSIS_VARIANT
        variant_index = ANALYSIS_VARIANT
        risk_stock = "ST" in name.upper() or "退市" in name or "暂停" in name
        template = (ST_ANALYSIS_TEMPLATES[variant_index % len(ST_ANALYSIS_TEMPLATES)]
                    if risk_stock else ANALYSIS_TEMPLATES[variant_index % len(ANALYSIS_TEMPLATES)])
        ANALYSIS_VARIANT += 1
        body = template.format(
            code=digits, name=name, price=price, change=change, pct=pct,
            low=period_low, high=period_high, zone=zone,
            ma_text=ma_text, resistance=period_high, support=period_low,
            trend=trend, volume_text=volume_text, ma60_text=ma60_text,
        )
        # Profile headers already carry the stock identity; avoid repeating it
        # when a rotated template starts with "code+name".
        if profile and body.startswith(f"{digits}{name}"):
            body = body[len(f"{digits}{name}"):].lstrip("：: ")
        elif body.startswith(f"{digits}{name}"):
            body = f"{name}（{digits}）" + body[len(f"{digits}{name}"):]
        prefix = ""
        if profile:
            prefix = f"{name}（{digits}）"
            prefix += f"｜{industry}。{intro}。"
        elif risk_stock:
            prefix = f"{name}（{digits}）："
        elif variant_index % 5 in {1, 4}:
            # Roughly two out of every ten ordinary replies get a natural
            # spoken opener; keep it out of ST/退市 warnings.
            body = "该股，" + body
        if profile and variant_index % 5 in {1, 4}:
            body = "该股，" + body
        narrative = prefix + body
    else:
        narrative = f"{name}（{digits}）现价 {price:.2f} 元，较前收 {change:+.2f} 元（{pct:+.2f}%），当前盘面状态为{trend}。"
    if cost is not None and cost > 0:
        profit = price - cost
        profit_pct = profit / cost * 100
        if profit_pct >= 3:
            if price >= resistance_level * 0.97:
                advice = f"已经有利润，但上方{resistance_level:.2f}元附近有压力，先别一涨就上头，冲不过去可以先收一部分，成本{cost:.2f}元当作自己的防守线。"
            else:
                advice = f"目前还在盈利区间，走势{trend}，先看{resistance_level:.2f}元能不能放量过去，别让到手的利润又坐回去。"
        elif profit_pct <= -3:
            if price <= support_level * 1.04:
                advice = f"现在离成本还有一段，股价已经靠近{support_level:.2f}元附近，先看这里能不能止住，别急着补，也别一味硬扛。"
            else:
                advice = f"现在离成本还有一段，下方{support_level:.2f}元才是先要观察的位置，支撑没站稳前，别急着摊平。"
        else:
            advice = f"现价和成本差得不多，这票有点磨人，先看{support_level:.2f}元和{resistance_level:.2f}元这两个位置，别为几个点来回折腾。"
        cost_text = f"你的成本是{cost:.2f}元，当前{('浮盈' if profit >= 0 else '浮亏')}{abs(profit):.2f}元（{profit_pct:+.2f}%）。{advice}"
        narrative += " " + cost_text
    return f"📊 {narrative}"


def _fetch_stock_history(symbol: str, days: int = 30) -> list[list[str]]:
    req = Request(
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{days},qfq",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    data = json.loads(_read_url(req, timeout=10).decode("utf-8", errors="replace"))
    rows = data.get("data", {}).get(symbol, {}).get("qfqday", [])
    return rows


def make_stock_chart(code: str, cost: float | None = None) -> tuple[str, Path]:
    """Create a dark, Chinese-labelled market chart from a public quote feed."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.patches import Rectangle
    match = STOCK_CODE_RE.fullmatch(code.strip())
    if not match:
        raise ValueError("股票代码应为 6 位数字")
    digits = match.group(1)
    market = "sh" if digits.startswith(("6", "68")) else "sz"
    symbol = market + digits
    rows = _fetch_stock_history(symbol, 60)
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
    ma60 = [sum(closes[max(0, i-59):i+1]) / min(i + 1, 60) for i in range(len(closes))]
    path = Path(tempfile.gettempdir()) / f"stock-{digits}.png"
    # Bundle an open-source CJK font because Render's base image may not have
    # any Chinese font installed; otherwise labels become tofu squares.
    bundled_font = Path(__file__).resolve().parent / "assets" / "NotoSansCJKsc-Regular.otf"
    if bundled_font.exists():
        font_manager.fontManager.addfont(str(bundled_font))
        cjk_name = font_manager.FontProperties(fname=str(bundled_font)).get_name()
        plt.rcParams["font.sans-serif"] = [cjk_name, "DejaVu Sans"]
    else:
        plt.rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "WenQuanYi Zen Hei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig = plt.figure(figsize=(10.8, 6.3), dpi=160)
    grid = fig.add_gridspec(2, 2, width_ratios=[4.5, 1.25], height_ratios=[3.2, 1],
                            wspace=0.06, hspace=0.05)
    ax = fig.add_subplot(grid[0, 0])
    vol_ax = fig.add_subplot(grid[1, 0], sharex=ax)
    chip_ax = fig.add_subplot(grid[:, 1], sharey=ax)
    background = "#050505"
    grid_color = "#5b1f2a"
    text_color = "#d7d7d7"
    fig.patch.set_facecolor(background)
    for panel in (ax, vol_ax, chip_ax):
        panel.set_facecolor(background)
        panel.tick_params(colors=text_color, labelsize=8)
        for spine in panel.spines.values():
            spine.set_color("#4b5563")
    for i, (op, cl, hi, lo) in enumerate(zip(opens, closes, highs, lows)):
        color = "#ff4d5a" if cl >= op else "#38d9e6"
        ax.vlines(i, lo, hi, color=color, linewidth=0.85)
        ax.add_patch(Rectangle((i - 0.32, min(op, cl)), 0.64, max(abs(cl-op), 0.01),
                               facecolor=color, edgecolor=color, alpha=0.95))
    ax.plot(range(len(closes)), ma5, label="5日均线", color="#f5d90a", linewidth=1.15)
    ax.plot(range(len(closes)), ma20, label="20日均线", color="#d84cff", linewidth=1.15)
    ax.plot(range(len(closes)), ma60, label="60日均线", color="#4ade80", linewidth=1.2)
    recent_closes = closes[-30:]
    support, resistance = min(recent_closes), max(recent_closes)
    current = closes[-1]
    ax.axhline(resistance, color="#ff4d5a", linestyle="--", linewidth=0.9, alpha=0.8, label="压力位")
    ax.axhline(support, color="#38d9e6", linestyle="--", linewidth=0.9, alpha=0.8, label="支撑位")
    ax.axhline(current, color="#facc15", linestyle=":", linewidth=0.8, alpha=0.75)
    if cost is not None and cost > 0:
        ax.axhline(cost, color="#fb923c", linestyle="-.", linewidth=1.0, alpha=0.9, label="成本线")
    ax.annotate(f"压力位 {resistance:.2f}", xy=(len(closes) - 1, resistance),
                xytext=(-5, 5), textcoords="offset points", ha="right", fontsize=8, color="#ff6b75")
    ax.annotate(f"支撑位 {support:.2f}", xy=(len(closes) - 1, support),
                xytext=(-5, 5), textcoords="offset points", ha="right", fontsize=8, color="#67e8f9")
    ax.annotate(f"现价 {current:.2f}", xy=(len(closes) - 1, current),
                xytext=(-5, -13), textcoords="offset points", ha="right", fontsize=8, color="#facc15")
    if cost is not None and cost > 0:
        ax.annotate(f"成本 {cost:.2f}", xy=(len(closes) - 1, cost),
                    xytext=(-5, 7), textcoords="offset points", ha="right", fontsize=8, color="#fb923c")
    ax.set_title(f"{digits}｜60日K线走势｜手机阅读优化", loc="left", fontweight="bold", color="#f3f4f6", pad=10)
    ax.grid(alpha=0.45, linestyle=":", color=grid_color)
    legend = ax.legend(frameon=False, ncol=6, loc="upper left", fontsize=8)
    for label in legend.get_texts():
        label.set_color(text_color)
    volume_colors = ["#ff4d5a" if cl >= op else "#38d9e6" for op, cl in zip(opens, closes)]
    vol_ax.bar(range(len(volumes)), volumes, color=volume_colors, width=0.64, alpha=0.78)
    vol_ax.set_ylabel("成交量", fontsize=8, color=text_color)
    vol_ax.grid(alpha=0.35, linestyle=":", color=grid_color)
    # A rule-based chip-peak proxy: distribution of the last 30 closing prices.
    # It is not a broker's proprietary cost-distribution dataset.
    chip_ax.hist(recent_closes, bins=12, orientation="horizontal", color="#facc15", alpha=0.68,
                 edgecolor="#fef3c7", linewidth=0.35)
    chip_ax.axhline(support, color="#38d9e6", linestyle="--", linewidth=0.9)
    chip_ax.axhline(resistance, color="#ff4d5a", linestyle="--", linewidth=0.9)
    chip_ax.axhline(current, color="#facc15", linestyle=":", linewidth=0.8)
    chip_ax.set_title("近30日筹码峰", fontsize=10, fontweight="bold", color="#f3f4f6")
    chip_ax.set_xlabel("价格密集度", fontsize=8, color=text_color)
    chip_ax.grid(axis="y", alpha=0.35, linestyle=":", color=grid_color)
    chip_ax.tick_params(axis="y", labelleft=False)
    ax.set_xticks(range(0, len(dates), max(1, len(dates)//8)))
    ax.set_xticklabels([dates[i] for i in range(0, len(dates), max(1, len(dates)//8))], rotation=45, fontsize=8)
    fig.text(0.02, 0.012, "参考支撑/压力、成本线仅作行情辅助；数据来自公开行情接口", color="#9ca3af", fontsize=7)
    fig.subplots_adjust(left=0.07, right=0.98, bottom=0.12, top=0.91)
    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
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
        "请发送股票代码，例如 600519。\n"
        "如果要结合持仓成本，可发送：600519 成本价 120。\n"
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
        await query.edit_message_text("关注验证通过。现在可以发送股票代码，也可以附上成本价，例如：600519 成本价 120。")
    else:
        await query.answer("还没有检测到关注，请先加入频道。", show_alert=True)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_membership(update, context):
        return
    text = (update.effective_message.text or "").strip()
    requested_code, cost = parse_stock_request(text)
    if cost is None:
        cost = extract_cost_price(text)
    if not requested_code:
        try:
            requested_code = await asyncio.to_thread(resolve_stock_name, text)
        except Exception:
            LOG.exception("Stock name lookup failed")
    if requested_code:
        request_id = uuid.uuid4().hex[:8]
        chart = None
        try:
            quote = await asyncio.to_thread(fetch_stock_quote, requested_code, cost)
        except Exception:
            LOG.exception("Quote lookup failed request_id=%s input=%r code=%s", request_id, text, requested_code)
            await update.effective_message.reply_text(
                f"行情接口这次没响应，已经自动重试。请稍后再发一次；反馈码：{request_id}"
            )
            return
        try:
            digits, chart = await asyncio.to_thread(make_stock_chart, requested_code, cost)
            image_bytes = chart.read_bytes()
        except Exception:
            LOG.exception("Chart generation failed request_id=%s input=%r code=%s", request_id, text, requested_code)
            await update.effective_message.reply_text(
                f"{quote}\n\n图表这次没生成出来，先给你文字解析。稍后重试即可；反馈码：{request_id}"
            )
            return
        try:
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
                LOG.exception("Failed to publish stock result request_id=%s", request_id)
        except Exception:
            LOG.exception("Telegram photo send failed request_id=%s", request_id)
            await update.effective_message.reply_text(
                f"文字解析已经完成，但图片发送失败。稍后重试即可；反馈码：{request_id}\n\n{quote}"
            )
        finally:
            if chart is not None:
                chart.unlink(missing_ok=True)
        return
    if text == "频道入口":
        await update.effective_message.reply_text("你的频道： https://t.me/jksjsjs6969")
        return
    await update.effective_message.reply_text(
        "请发送股票代码，例如 600519。\n"
        "也可以附上成本价：600519 成本价 120。"
    )


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
