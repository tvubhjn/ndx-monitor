#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NDX-100 成分股监控器（Gate.io 永续合约版）

监控范围：只监控「纳斯达克100指数（NDX 100）成分股」中「Gate.io 已上架永续合约」的那部分，
外加宽基 ETF（QQQ / SPY）。其余标的（中概股、日韩台股、Gate 未上架的 NDX 成分）一律不监控。

背景：
- 币安合约 API（fapi.binance.com）屏蔽美国 IP（HTTP 451），GitHub Actions 在美国，用不了。
- OKX 在美可达，但股票/ETF 合约 227 个太多、噪音大。
- Gate.io 公开行情接口在美可达，且合约有 contract_type 字段可精准区分股票（stocks）。

特性：
- 标的：固定名单（config.json 的 stocks = NDX100 ∩ Gate 股票合约；index_symbols = QQQ/SPY）
- 数据源：Gate.io 公开行情接口，免费、无需 key，一次调用拿全量 ticker
- 分层阈值：宽基 ETF ±1.3%，个股 ±3%，24h 振幅 ≥5%
- 时段频率：盘前每 5 分钟 / 盘后每 10 分钟 / 盘中每 15 分钟 / 休市不跑
- 冷却：同一标的 60 分钟内不重复告警
- 零第三方依赖：仅用 Python 标准库

用法：
  python monitor.py            正常监控
  python monitor.py --check    忽略时段/频率，强制取价打印诊断
  python monitor.py --test     取价 + 发一封测试邮件

配置：config.json（阈值、股票名单、宽基ETF、收件人）
密钥：环境变量 SMTP_USER / SMTP_PASSWORD（QQ 邮箱授权码）
"""

import json
import os
import sys
import time
import ssl
import smtplib
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from urllib import request
from urllib.error import URLError, HTTPError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_DIR = os.path.join(BASE_DIR, "state")
ALERTS_PATH = os.path.join(STATE_DIR, "alerts.json")
LAST_RUN_PATH = os.path.join(STATE_DIR, "last_run.json")

# 时区：优先用系统 tzdata；Windows 本地若缺 tzdata 则退化为固定偏移（仅本地调试用）
try:
    from zoneinfo import ZoneInfo
    ET_TZ = ZoneInfo("America/New_York")
    BJ_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001
    from datetime import timezone, timedelta
    ET_TZ = timezone(timedelta(hours=-5))  # 美东冬令时 EST（夏令时需 -4）
    BJ_TZ = timezone(timedelta(hours=8))
    print("[提示] 未找到 tzdata，美东时区按固定 -5 处理（仅本地调试；GitHub Actions 不受影响）")

# Gate.io USDT 永续合约公开接口（无需 key，美国 IP 可访问）
API_CONTRACTS = "https://api.gateio.ws/api/v4/futures/usdt/contracts"
API_TICKERS = "https://api.gateio.ws/api/v4/futures/usdt/tickers"

CONTRACT_SUFFIX = "_USDT"


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def http_get_json(url, timeout=20):
    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def fnum(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# 时段判断（美东时间，自动处理夏令时）
# --------------------------------------------------------------------------

def market_session(now_et):
    t = now_et.hour * 60 + now_et.minute
    if 4 * 60 <= t < 9 * 60 + 30:
        return "premarket"
    if 9 * 60 + 30 <= t < 16 * 60:
        return "regular"
    if 16 * 60 <= t < 20 * 60:
        return "afterhours"
    return "closed"


SESSION_LABEL = {"premarket": "盘前", "regular": "盘中", "afterhours": "盘后", "closed": "休市"}
SESSION_INTERVAL = {"premarket": 5, "afterhours": 10, "regular": 15}


def should_run(session, force=False):
    if force:
        return True
    interval = SESSION_INTERVAL.get(session)
    if interval is None:
        return False
    last = load_json(LAST_RUN_PATH, 0)
    if isinstance(last, dict):
        last = last.get("t", 0)
    return (time.time() - last) >= interval * 60 - 30


# --------------------------------------------------------------------------
# 获取 Gate.io 股票/ETF 合约 + 行情
# --------------------------------------------------------------------------

def get_live_stock_bases():
    """返回 Gate 当前正在交易、且 contract_type=stocks 的标的 base 集合（去掉 _USDT）。"""
    data = http_get_json(API_CONTRACTS)
    bases = set()
    for c in data:
        if c.get("contract_type") == "stocks" and c.get("status") == "trading":
            name = c.get("name", "")
            if name.endswith(CONTRACT_SUFFIX):
                bases.add(name[: -len(CONTRACT_SUFFIX)])
    return bases


def fetch_tickers():
    """一次调用返回全部 USDT 永续 ticker，{base: ticker_dict}。"""
    arr = http_get_json(API_TICKERS)
    out = {}
    for t in arr:
        if not isinstance(t, dict):
            continue
        name = t.get("contract", "")
        if name.endswith(CONTRACT_SUFFIX):
            out[name[: -len(CONTRACT_SUFFIX)]] = t
    return out


def build_watchlist(cfg, live_bases):
    """config 名单 ∩ Gate 在交易合约。返回 [(base, is_index)]，指数/ETF 优先。"""
    watch = []
    seen = set()
    for s in cfg.get("index_symbols", []):
        if s in live_bases and s not in seen:
            watch.append((s, True))
            seen.add(s)
    for s in cfg.get("stocks", []):
        if s in live_bases and s not in seen:
            watch.append((s, False))
            seen.add(s)
    return watch


# --------------------------------------------------------------------------
# 告警判定（Gate 字段：last / change_percentage / high_24h / low_24h）
# --------------------------------------------------------------------------

def evaluate(base, tk, is_index, thr):
    last = fnum(tk.get("last"))
    chg = fnum(tk.get("change_percentage"))
    high = fnum(tk.get("high_24h"))
    low = fnum(tk.get("low_24h"))
    volume = fnum(tk.get("volume_24h_quote"))

    if last is None or last <= 0 or chg is None:
        return None

    # 由 24h 涨跌幅反推 24h 前价格，用于计算振幅
    prev = None
    if chg > -99.0:
        prev = last / (1.0 + chg / 100.0)
    amplitude = None
    if high is not None and low is not None and prev and prev > 0:
        amplitude = (high - low) / prev * 100.0

    threshold = thr.get("index_change_pct", 1.3) if is_index else thr.get("stock_change_pct", 3.0)

    direction = None
    if chg >= threshold:
        direction = "up"
    elif chg <= -threshold:
        direction = "down"

    amp_hit = amplitude is not None and amplitude >= thr.get("amplitude_pct", 5.0)

    if not direction and not amp_hit:
        return None

    return {
        "symbol": base,
        "price": last,
        "change_pct": chg,
        "amplitude_pct": amplitude,
        "volume": volume,
        "is_index": is_index,
        "amp_hit": amp_hit,
        "direction": direction,
    }


# --------------------------------------------------------------------------
# 邮件
# --------------------------------------------------------------------------

def build_body(events, session, now_bj):
    # 涨的在前（涨幅从大到小），跌的在后（跌幅从大到小）
    ups = sorted([e for e in events if e.get("direction") == "up"],
                 key=lambda e: -(e.get("change_pct") or 0))
    downs = sorted([e for e in events if e.get("direction") == "down"],
                   key=lambda e: e.get("change_pct") or 0)
    others = [e for e in events if e.get("direction") not in ("up", "down")]

    lines = [
        "NDX-100 股票监控告警（Gate 永续）",
        "=" * 28,
        "",
        "时段：%s（北京时间 %s）" % (SESSION_LABEL.get(session, session), now_bj.strftime("%Y-%m-%d %H:%M:%S")),
        "",
    ]

    def block(title, arr):
        if not arr:
            return
        lines.append("%s（%d 个）" % (title, len(arr)))
        for e in arr:
            lines.append("  %-6s %+.2f%%" % (e["symbol"], e["change_pct"]))
        lines.append("")

    block("上涨", ups)
    block("下跌", downs)
    block("大幅震荡（仅振幅触发）", others)

    lines.append("数据源：Gate.io 股票/ETF 永续（24/7）。本邮件由 GitHub Actions 自动发送，请勿直接回复。")
    return "\n".join(lines)


def send_email(cfg, subject, body):
    smtp = cfg.get("smtp", {})
    user = os.environ.get("SMTP_USER") or smtp.get("user", "")
    password = os.environ.get("SMTP_PASSWORD") or smtp.get("password", "")
    to_raw = os.environ.get("EMAIL_TO") or cfg.get("email", {}).get("to", [])
    if isinstance(to_raw, str):
        to_list = [x.strip() for x in to_raw.split(",") if x.strip()]
    else:
        to_list = [str(x) for x in to_raw]

    if not user or not password:
        print("[邮件] 未配置 SMTP 账号/授权码，跳过发送。")
        return False
    if not to_list:
        print("[邮件] 未配置收件人，跳过发送。")
        return False

    host = smtp.get("host", "smtp.qq.com")
    port = int(smtp.get("port", 465))

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = ", ".join(to_list)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as server:
        server.login(user, password)
        server.sendmail(user, to_list, msg.as_string())
    print("[邮件] 已发送 -> %s" % ", ".join(to_list))
    return True


# --------------------------------------------------------------------------
# 核心流程
# --------------------------------------------------------------------------

def run_monitor(force=False):
    cfg = load_config()
    thr = cfg.get("thresholds", {})

    now_et = datetime.now(ET_TZ)
    now_bj = datetime.now(BJ_TZ)
    session = market_session(now_et)

    if not force:
        if session == "closed":
            print("[时段] 美股休市中，跳过（美东 %s）。" % now_et.strftime("%H:%M"))
            return
        if not should_run(session):
            print("[节流] %s时段未到运行间隔，跳过。" % SESSION_LABEL[session])
            return

    save_json(LAST_RUN_PATH, {"t": int(time.time()), "session": session})

    try:
        live_bases = get_live_stock_bases()
        tickers = fetch_tickers()
    except (URLError, HTTPError, OSError, ValueError) as e:
        print("[错误] 获取 Gate 行情失败：%s" % str(e)[:120])
        return

    watch = build_watchlist(cfg, live_bases)
    if not watch:
        print("[提示] 监控名单为空（config.stocks 与 Gate 在交易合约无交集）。")
        return

    alerts = load_json(ALERTS_PATH, {})
    now = time.time()
    cooldown = int(cfg.get("cooldown_minutes", 60))

    events = []
    checked = 0
    missing = []
    for base, is_index in watch:
        tk = tickers.get(base)
        if not tk:
            missing.append(base)
            continue
        checked += 1
        ev = evaluate(base, tk, is_index, thr)
        if not ev:
            continue

        last_alert = alerts.get(base, {})
        if (now - last_alert.get("t", 0)) < cooldown * 60:
            print("[%s] 触发但处于冷却期，已抑制" % base)
            continue

        events.append(ev)
        alerts[base] = {"t": int(now), "dir": ev.get("direction", "amp"), "p": ev["price"]}
        if ev.get("direction"):
            print("[%s] %s %+.2f%% 触发告警" % (base, "指数/ETF" if is_index else "个股", ev["change_pct"]))
        if ev.get("amp_hit"):
            print("[%s] 24h振幅 %.2f%% 触发告警" % (base, ev["amplitude_pct"]))

    # 清理已不在名单内的冷却记录
    watch_bases = {b for b, _ in watch}
    alerts = {k: v for k, v in alerts.items() if k in watch_bases}
    save_json(ALERTS_PATH, alerts)

    print("== 监控运行 @ %s（美东 %s，%s）== 标的 %d 个，有行情 %d 个 =="
          % (now_bj.strftime("%Y-%m-%d %H:%M:%S"), now_et.strftime("%H:%M"),
             SESSION_LABEL[session], len(watch), checked))
    if missing:
        print("[提示] 以下标的本轮无行情（可能停牌/刚上架）：%s" % " ".join(missing))

    if events:
        subject = "【NDX股票告警】%d 个标的触发波动阈值（%s）" % (len(events), SESSION_LABEL[session])
        send_email(cfg, subject, build_body(events, session, now_bj))
    else:
        print("本轮无标的触发阈值。")


def run_check():
    cfg = load_config()
    now_et = datetime.now(ET_TZ)
    session = market_session(now_et)
    try:
        live_bases = get_live_stock_bases()
        tickers = fetch_tickers()
    except (URLError, HTTPError, OSError, ValueError) as e:
        print("[错误] 获取 Gate 行情失败：%s" % str(e)[:120])
        return

    watch = build_watchlist(cfg, live_bases)
    cfg_stocks = cfg.get("stocks", [])
    cfg_index = cfg.get("index_symbols", [])
    not_listed = [s for s in cfg_stocks if s not in live_bases]

    print("== 自检：Gate 在交易股票合约 %d 个 | 配置名单 个股%d + 指数%d =="
          % (len(live_bases), len(cfg_stocks), len(cfg_index)))
    print("== 实际监控 %d 个（时段=%s）==" % (len(watch), SESSION_LABEL[session]))
    if not_listed:
        print("[提示] 配置里但 Gate 未上架/停牌：%s" % " ".join(not_listed))

    rows = []
    for base, is_index in watch:
        tk = tickers.get(base)
        if not tk:
            continue
        rows.append((base, is_index, tk))

    def key_of(r):
        c = fnum(r[2].get("change_percentage"))
        return abs(c) if c is not None else 0

    rows.sort(key=key_of, reverse=True)
    print("---- 按 24h 涨跌幅绝对值排序 ----")
    for base, is_index, tk in rows:
        last = fnum(tk.get("last"))
        chg = fnum(tk.get("change_percentage"))
        high = fnum(tk.get("high_24h"))
        low = fnum(tk.get("low_24h"))
        prev = (last / (1.0 + chg / 100.0)) if (last and chg is not None and chg > -99.0) else None
        amp = ((high - low) / prev * 100.0) if (high is not None and low is not None and prev and prev > 0) else None
        tag = "指数/ETF" if is_index else "个股"
        print("[%s] %-5s 现价=%-9s 24h涨跌=%s 振幅=%s" % (
            base, tag, ("%.4g" % last) if last else "-",
            ("%+.2f%%" % chg) if chg is not None else "-",
            ("%.2f%%" % amp) if amp is not None else "-"))
    print("== 共 %d 个标的 ==" % len(rows))


def run_test():
    cfg = load_config()
    thr = cfg.get("thresholds", {})
    now_et = datetime.now(ET_TZ)
    now_bj = datetime.now(BJ_TZ)
    session = market_session(now_et)
    try:
        live_bases = get_live_stock_bases()
        tickers = fetch_tickers()
    except (URLError, HTTPError, OSError, ValueError) as e:
        print("[错误] 获取 Gate 行情失败：%s" % str(e)[:120])
        return

    watch = build_watchlist(cfg, live_bases)
    hits = []
    for base, is_index in watch:
        tk = tickers.get(base)
        if not tk:
            continue
        ev = evaluate(base, tk, is_index, thr)
        if ev:
            hits.append(ev)

    lines = ["NDX-100 股票监控测试邮件（Gate 永续）", "=" * 28, "",
             "时段：%s" % SESSION_LABEL[session],
             "监控标的：%d 个（Gate 在交易且属于 NDX100/宽基ETF）" % len(watch),
             ""]

    if hits:
        ups = sorted([e for e in hits if e.get("direction") == "up"],
                     key=lambda e: -(e.get("change_pct") or 0))
        downs = sorted([e for e in hits if e.get("direction") == "down"],
                       key=lambda e: e.get("change_pct") or 0)
        others = [e for e in hits if e.get("direction") not in ("up", "down")]
        lines.append("以下 %d 个标的当前涨跌超阈值：" % len(hits))
        lines.append("")

        def block(title, arr):
            if not arr:
                return
            lines.append("%s（%d 个）" % (title, len(arr)))
            for e in arr:
                lines.append("  %-6s %+.2f%%" % (e["symbol"], e["change_pct"]))
            lines.append("")

        block("上涨", ups)
        block("下跌", downs)
        block("大幅震荡", others)
    else:
        lines.append("当前无标的涨跌超阈值。以下为波动最大的前 15 个样本：")
        lines.append("")
        rows = []
        for base, is_index in watch:
            tk = tickers.get(base)
            if tk:
                rows.append((base, is_index, tk))
        rows.sort(key=lambda r: abs(fnum(r[2].get("change_percentage")) or 0), reverse=True)
        for base, is_index, tk in rows[:15]:
            last = fnum(tk.get("last"))
            chg = fnum(tk.get("change_percentage"))
            lines.append("%s %s 现价=%s 24h涨跌=%s" % (
                base, "指数/ETF" if is_index else "个股", ("%.4g" % last) if last else "-",
                ("%+.2f%%" % chg) if chg is not None else "-"))

    lines.append("-" * 28)
    lines.append("收到此邮件 = 取价 + 发信链路已打通。")
    lines.append("时间：%s" % now_bj.strftime("%Y-%m-%d %H:%M:%S"))
    body = "\n".join(lines)
    print(body)
    sent = send_email(cfg, "【NDX股票监控】链路测试（Gate）", body)
    print("[测试] 邮件发送：%s" % ("成功" if sent else "未发送（缺 SMTP 配置）"))


def main():
    if "--check" in sys.argv:
        run_check()
    elif "--test" in sys.argv:
        run_test()
    else:
        run_monitor()


if __name__ == "__main__":
    main()
