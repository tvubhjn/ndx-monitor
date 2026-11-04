#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
币安股票/ETF 永续合约监控器（TradFi Perps）

监控币安上架的所有股票/ETF 永续合约（TSLAUSDT、NVDAUSDT、QQQUSDT 等），
覆盖美股盘前 / 盘中 / 盘后，按「24h 涨跌幅」和「24h 振幅」超阈值发邮件告警。

特性：
- 标的动态获取：从币安 exchangeInfo 自动筛出 underlyingType=EQUITY 的合约，
  币安上新自动覆盖，无需手动维护名单
- 数据源：币安 fapi 公开行情接口，无需任何 API key
- 一次调用拿全量行情：ticker/24hr 接口返回所有合约，无需逐个查询
- 分层阈值：宽基 ETF/指数 ±0.4%，个股 ±3%，24h 振幅 ≥5%
- 时段频率：盘前每 5 分钟 / 盘后每 10 分钟 / 盘中每 15 分钟 / 休市不跑
- 冷却：同一标的 60 分钟内不重复告警
- 零第三方依赖：仅用 Python 标准库，GitHub Actions 可直接跑

用法：
  python monitor.py            正常监控（判断时段 -> 取价 -> 判阈值 -> 告警）
  python monitor.py --check    忽略时段/频率，强制取价打印诊断（本地自测）
  python monitor.py --test     取价 + 发一封测试邮件（验证邮箱链路）

配置：config.json（阈值、频率、宽基ETF列表、收件人）
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
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_DIR = os.path.join(BASE_DIR, "state")
ALERTS_PATH = os.path.join(STATE_DIR, "alerts.json")
LAST_RUN_PATH = os.path.join(STATE_DIR, "last_run.json")

ET_TZ = ZoneInfo("America/New_York")
BJ_TZ = ZoneInfo("Asia/Shanghai")

API_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
API_TICKER_24H = "https://fapi.binance.com/fapi/v1/ticker/24hr"

# 股票/ETF 合约的 underlyingType 保留值（EQUITY=股票/ETF，INDEX 为可能的指数值）
EQUITY_TYPES = {"EQUITY", "ETF", "INDEX", "STOCK"}
# 明确排除的标的类型
EXCLUDE_TYPES = {"CRYPTO", "COMMODITY"}


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def http_get_json(url, timeout=20):
    req = request.Request(url, headers={"User-Agent": "ndx-monitor", "Accept": "application/json"})
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
    """返回 'premarket' / 'regular' / 'afterhours' / 'closed'"""
    t = now_et.hour * 60 + now_et.minute
    if 4 * 60 <= t < 9 * 60 + 30:      # 盘前 4:00 - 9:30
        return "premarket"
    if 9 * 60 + 30 <= t < 16 * 60:     # 盘中 9:30 - 16:00
        return "regular"
    if 16 * 60 <= t < 20 * 60:         # 盘后 16:00 - 20:00
        return "afterhours"
    return "closed"


SESSION_LABEL = {
    "premarket": "盘前",
    "regular": "盘中",
    "afterhours": "盘后",
    "closed": "休市",
}

SESSION_INTERVAL = {
    "premarket": 5,
    "afterhours": 10,
    "regular": 15,
}


def should_run(session, force=False):
    if force:
        return True
    interval = SESSION_INTERVAL.get(session)
    if interval is None:
        return False
    last = load_json(LAST_RUN_PATH, 0)
    if isinstance(last, dict):
        last = last.get("t", 0)
    return (time.time() - last) >= interval * 60 - 30  # 留 30 秒余量


# --------------------------------------------------------------------------
# 获取币安股票/ETF 永续合约
# --------------------------------------------------------------------------

def get_equity_symbols():
    """从 exchangeInfo 筛出股票/ETF 永续合约的 symbol 集合。"""
    data = http_get_json(API_EXCHANGE_INFO)
    symbols = set()
    for s in data.get("symbols", []):
        if s.get("quoteAsset") != "USDT":
            continue
        if s.get("contractType") != "PERPETUAL":
            continue
        if s.get("status") != "TRADING":
            continue
        utype = s.get("underlyingType", "")
        if utype in EQUITY_TYPES:
            symbols.add(s["symbol"])
        elif utype in EXCLUDE_TYPES or utype == "":
            continue
        else:
            # 未知类型：兜底，按"非加密币 symbol"处理不了的场景跳过，保持只监控明确 EQUITY
            continue
    return symbols


# --------------------------------------------------------------------------
# 获取行情（一次调用拿全量 24h 数据）
# --------------------------------------------------------------------------

def fetch_tickers():
    """返回 {symbol: ticker_dict}，ticker 含 lastPrice/priceChangePercent/high/low/prevClose。"""
    arr = http_get_json(API_TICKER_24H)
    return {t["symbol"]: t for t in arr if isinstance(t, dict) and t.get("symbol")}


# --------------------------------------------------------------------------
# 告警判定
# --------------------------------------------------------------------------

def evaluate(symbol, tk, is_index, thr):
    """返回事件 dict 或 None。"""
    last = fnum(tk.get("lastPrice"))
    pcp = fnum(tk.get("priceChangePercent"))
    high = fnum(tk.get("highPrice"))
    low = fnum(tk.get("lowPrice"))
    prev = fnum(tk.get("prevClosePrice"))

    if last is None:
        return None
    if pcp is None and prev and prev > 0:
        pcp = (last - prev) / prev * 100

    threshold = thr["index_change_pct"] if is_index else thr["stock_change_pct"]

    direction = None
    if pcp is not None:
        if pcp >= threshold:
            direction = "up"
        elif pcp <= -threshold:
            direction = "down"

    amplitude = None
    if high is not None and low is not None and prev and prev > 0:
        amplitude = (high - low) / prev * 100
    amp_hit = amplitude is not None and amplitude >= thr["amplitude_pct"]

    if not direction and not amp_hit:
        return None

    ev = {
        "symbol": symbol,
        "price": last,
        "change_pct": pcp if pcp is not None else 0.0,
        "amplitude_pct": amplitude,
        "is_index": is_index,
        "amp_hit": amp_hit,
    }
    if direction:
        ev["direction"] = direction
    return ev


# --------------------------------------------------------------------------
# 邮件
# --------------------------------------------------------------------------

def build_body(events, session, now_bj):
    lines = [
        "币安股票/ETF 永续监控告警",
        "=" * 28,
        "",
        "时段：%s（北京时间 %s）" % (SESSION_LABEL.get(session, session), now_bj.strftime("%Y-%m-%d %H:%M:%S")),
        "",
    ]
    for ev in events:
        if ev.get("direction") == "up":
            arrow = "↑ 上涨"
        elif ev.get("direction") == "down":
            arrow = "↓ 下跌"
        else:
            arrow = "↕ 大幅震荡"
        lines.append("%s %s" % (ev["symbol"], arrow))
        lines.append("  现价：%.4g" % ev["price"])
        lines.append("  24h涨跌幅：%+.2f%%" % ev["change_pct"])
        if ev["amplitude_pct"] is not None:
            lines.append("  24h振幅：%.2f%%" % ev["amplitude_pct"])
        lines.append("-" * 28)
    lines.append("")
    lines.append("数据源：币安合约（TradFi 永续，24/7）。本邮件由 GitHub Actions 自动发送，请勿直接回复。")
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
    index_set = set(cfg.get("index_symbols", []))

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
        equity_symbols = get_equity_symbols()
        tickers = fetch_tickers()
    except (URLError, HTTPError, OSError, ValueError) as e:
        print("[错误] 获取行情失败：%s" % str(e)[:120])
        return

    if not equity_symbols:
        print("[提示] 未获取到股票/ETF 合约，请检查网络或币安接口。")
        return

    alerts = load_json(ALERTS_PATH, {})
    now = time.time()
    cooldown = int(cfg.get("cooldown_minutes", 60))

    events = []
    checked = 0
    for symbol in sorted(equity_symbols):
        tk = tickers.get(symbol)
        if not tk:
            continue
        checked += 1
        # 去 USDT 后缀取股票代码，判断是否宽基 ETF/指数
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        is_index = base in index_set
        ev = evaluate(symbol, tk, is_index, thr)
        if not ev:
            continue

        last = alerts.get(symbol, {})
        if (now - last.get("t", 0)) < cooldown * 60:
            print("[%s] 触发但处于冷却期，已抑制" % symbol)
            continue

        events.append(ev)
        alerts[symbol] = {"t": int(now), "dir": ev.get("direction", "amp"), "p": ev["price"]}
        if ev.get("direction"):
            print("[%s] %s %+.2f%% 触发告警" % (symbol, "指数" if is_index else "个股", ev["change_pct"]))
        if ev.get("amp_hit"):
            print("[%s] 24h振幅 %.2f%% 触发告警" % (symbol, ev["amplitude_pct"]))

    save_json(ALERTS_PATH, alerts)

    print("== 监控运行 @ %s（美东 %s，%s）== 监控标的 %d 个 =="
          % (now_bj.strftime("%Y-%m-%d %H:%M:%S"), now_et.strftime("%H:%M"),
             SESSION_LABEL[session], checked))

    if events:
        subject = "【币安股票合约告警】%d 个标的触发波动阈值（%s）" % (len(events), SESSION_LABEL[session])
        send_email(cfg, subject, build_body(events, session, now_bj))
    else:
        print("本轮无标的触发阈值。")


def run_check():
    cfg = load_config()
    thr = cfg.get("thresholds", {})
    index_set = set(cfg.get("index_symbols", []))
    now_et = datetime.now(ET_TZ)
    session = market_session(now_et)
    try:
        equity_symbols = get_equity_symbols()
        tickers = fetch_tickers()
    except (URLError, HTTPError, OSError, ValueError) as e:
        print("[错误] 获取行情失败：%s" % str(e)[:120])
        return

    print("== 自检：币安股票/ETF 永续合约 %d 个（时段=%s）==" % (len(equity_symbols), SESSION_LABEL[session]))
    rows = []
    for symbol in sorted(equity_symbols):
        tk = tickers.get(symbol)
        if not tk:
            continue
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        rows.append((base, tk))
    # 按 24h 涨跌幅绝对值降序，打印最活跃的前 30 个
    def key_of(r):
        pcp = fnum(r[1].get("priceChangePercent"))
        return abs(pcp) if pcp is not None else 0
    rows.sort(key=key_of, reverse=True)
    for base, tk in rows[:30]:
        pcp = fnum(tk.get("priceChangePercent"))
        last = fnum(tk.get("lastPrice"))
        high = fnum(tk.get("highPrice"))
        low = fnum(tk.get("lowPrice"))
        prev = fnum(tk.get("prevClosePrice"))
        amp = (high - low) / prev * 100 if high and low and prev else None
        tag = "指数" if base in index_set else "个股"
        print("[%s] %s 现价=%.4g 24h涨跌=%+.2f%% 振幅=%s" % (
            base, tag, last if last else 0,
            pcp if pcp is not None else 0,
            "%.2f%%" % amp if amp is not None else "-"))
    print("== 共 %d 个标的 ==" % len(rows))


def run_test():
    cfg = load_config()
    now_et = datetime.now(ET_TZ)
    now_bj = datetime.now(BJ_TZ)
    session = market_session(now_et)
    try:
        equity_symbols = get_equity_symbols()
        tickers = fetch_tickers()
    except (URLError, HTTPError, OSError, ValueError) as e:
        print("[错误] 获取行情失败：%s" % str(e)[:120])
        return

    lines = ["币安股票合约监控测试邮件", "=" * 28, "", "时段：%s" % SESSION_LABEL[session]]
    for symbol in sorted(equity_symbols)[:8]:
        tk = tickers.get(symbol)
        if not tk:
            continue
        pcp = fnum(tk.get("priceChangePercent"))
        last = fnum(tk.get("lastPrice"))
        lines.append("%s 现价=%.4g 24h涨跌=%+.2f%%" % (
            symbol, last if last else 0, pcp if pcp is not None else 0))
    lines.append("-" * 28)
    lines.append("收到此邮件 = 取价 + 发信链路已打通。")
    lines.append("时间：%s" % now_bj.strftime("%Y-%m-%d %H:%M:%S"))
    body = "\n".join(lines)
    print(body)
    sent = send_email(cfg, "【币安股票合约监控】链路测试", body)
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
