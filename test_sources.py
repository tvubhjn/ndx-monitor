#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断2：dump OKX 上架的股票/ETF 永续合约清单，确定区分字段。"""
import json
from urllib import request
from urllib.error import HTTPError, URLError

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# 常见加密币（用于排除，剩下的就是股票/ETF/指数）
CRYPTO = {"BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "DOT", "LINK",
          "LTC", "BCH", "NEAR", "APT", "ARB", "OP", "SUI", "PEPE", "SHIB", "UNI",
          "AAVE", "ATOM", "ETC", "FIL", "ICP", "TRX", "XLM", "EOS", "TIA", "SEI",
          "INJ", "WIF", "ORDI", "1000PEPE", "1000SHIB", "1000BONK"}

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
    print("OKX SWAP 合约总数:", len(insts))
    # 看一个样例的完整字段
    if insts:
        print("\n样例字段（加密币 BTC）:")
        print(json.dumps(insts[0], ensure_ascii=False, indent=2)[:800])

    # 找出股票/ETF 类（baseCcy 不在加密币集合里，且 quoteCcy=USDT）
    equities = []
    for s in insts:
        base = s.get("ctValCcy") or s.get("baseCcy") or ""
        inst_id = s.get("instId", "")
        # instId 形如 TSLA-USDT-SWAP
        parts = inst_id.split("-")
        base2 = parts[0] if parts else ""
        quote = parts[1] if len(parts) > 1 else ""
        if quote == "USDT" and base2 not in CRYPTO:
            equities.append((base2, inst_id, s.get("ctType"), s.get("settleCcy")))

    print("\n非加密币的 USDT 永续合约（股票/ETF/指数候选）共 %d 个:" % len(equities))
    for b, iid, ct, sc in equities:
        print("  %-12s %-28s ctType=%s settle=%s" % (b, iid, ct, sc))


if __name__ == "__main__":
    main()
