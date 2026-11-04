#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性诊断脚本：测试 GitHub Actions(美国IP) 上各交易所 API 可达性。"""
import json
from urllib import request
from urllib.error import HTTPError, URLError

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

TESTS = [
    ("币安合约-单只ticker", "https://fapi.binance.com/fapi/v1/ticker/price?symbol=TSLAUSDT"),
    ("币安合约-exchangeInfo", "https://fapi.binance.com/fapi/v1/exchangeInfo"),
    ("币安合约-ticker24hr", "https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=TSLAUSDT"),
    ("币安现货镜像-ping", "https://data-api.binance.vision/api/v3/ping"),
    ("币安现货镜像-股票ticker", "https://data-api.binance.vision/api/v3/ticker/24hr?symbol=TSLAUSDT"),
    ("OKX-swap", "https://www.okx.com/api/v5/market/ticker?instId=TSLA-USDT-SWAP"),
    ("OKX-aws节点", "https://aws.okx.com/api/v5/market/ticker?instId=TSLA-USDT-SWAP"),
    ("Bybit-linear", "https://api.bybit.com/v5/market/tickers?category=linear&symbol=TSLAUSDT"),
    ("Gate-futures", "https://api.gateio.ws/api/v4/futures/usdt/tickers?contract=TSLA_USDT"),
]


def probe(url):
    req = request.Request(url, headers=UA)
    try:
        with request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "ignore")
            return "HTTP %d  前120字: %s" % (resp.status, body[:120].replace("\n", " "))
    except HTTPError as e:
        return "HTTP %d (%s)  %s" % (e.code, e.reason, e.read().decode("utf-8", "ignore")[:100])
    except URLError as e:
        return "URLError: %s" % e.reason
    except Exception as e:  # noqa: BLE001
        return "Exception: %s" % str(e)[:100]


if __name__ == "__main__":
    print("=" * 70)
    print("GitHub Actions 环境交易所 API 可达性测试")
    print("=" * 70)
    for name, url in TESTS:
        print("\n[%s]\n  %s\n  -> %s" % (name, url, probe(url)))
    print("\n" + "=" * 70)
