#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断3：对比 OKX 股票永续 vs 加密币永续的字段差异。"""
import json
from urllib import request
from urllib.error import HTTPError, URLError

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
URL = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"


def main():
    req = request.Request(URL, headers=UA)
    try:
        with request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, Exception) as e:  # noqa: BLE001
        print("获取失败:", str(e)[:150])
        return

    insts = data.get("data", [])
    targets = ["AAPL-USDT-SWAP", "QQQ-USDT-SWAP", "TSLA-USDT-SWAP", "BTC-USDT-SWAP", "BTC-USD-SWAP"]
    for t in targets:
        hit = next((s for s in insts if s.get("instId") == t), None)
        if not hit:
            print("\n[%s] 未找到" % t)
            continue
        print("\n[%s] 关键字段:" % t)
        for k in ["instType", "instId", "uly", "instFamily", "baseCcy", "quoteCcy",
                  "settleCcy", "ctValCcy", "ctType", "instCategory", "category", "groupId",
                  "state", "lever"]:
            print("   %-14s = %s" % (k, hit.get(k)))


if __name__ == "__main__":
    main()
