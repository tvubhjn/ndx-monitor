# 币安股票/ETF 永续合约监控器

监控**币安上架的所有股票/ETF 永续合约**（`TSLAUSDT`、`NVDAUSDT`、`QQQUSDT` 等），覆盖美股**盘前 / 盘中 / 盘后**，按「24h 涨跌幅」和「24h 振幅」超阈值发邮件到你的 QQ 邮箱。

- **标的自动更新**：脚本从币安 `exchangeInfo` 动态筛出所有 `underlyingType=EQUITY` 的合约，币安上新自动覆盖，无需手动维护名单。
- **无需任何 API key**：币安合约公开行情接口，连注册都省了。
- **一次调用拿全量**：`ticker/24hr` 接口返回所有合约行情，比逐个查询高效得多。
- **24 小时运行**：跑在 GitHub 云服务器，你电脑关不关机都行。

---

## 一、文件结构

| 文件 | 作用 |
|------|------|
| `monitor.py` | 核心脚本（筛合约、取行情、判阈值、发邮件） |
| `config.json` | 阈值、宽基 ETF 列表、收件人 |
| `.github/workflows/monitor.yml` | 定时运行（盘前5分钟/盘后10分钟/盘中15分钟） |
| `state/` | 冷却状态（自动生成，别删） |

## 二、监控规则（默认参数）

| 对象 | 涨跌幅阈值 | 振幅阈值 |
|------|-----------|---------|
| 宽基 ETF（QQQ、SPY） | ±0.4% | — |
| 个股 | ±3% | ≥5% |

- **涨跌幅** = 币安永续 24h 涨跌幅（`priceChangePercent`）
- **振幅** = 24h 最高 − 最低，相对 24h 前收盘
- **冷却**：同一标的触发后 60 分钟内不重复告警

## 三、时段频率（美东时间，自动适配夏令时）

| 时段 | 美东时间 | 北京对应 | 频率 |
|------|---------|---------|------|
| 盘前 | 4:00–9:30 | 17:00–22:30 | 每 5 分钟 |
| 盘中 | 9:30–16:00 | 22:30–次日5:00 | 每 15 分钟 |
| 盘后 | 16:00–20:00 | 5:00–9:00 | 每 10 分钟 |
| 休市 | 其余 | — | 不跑 |

## 四、部署到 GitHub（网页操作，全程免费）

### 第 1 步：建仓库

登录 github.com → 右上角 `+` → `New repository` → 名称填 `ndx-monitor` → 选 **Public** → `Create repository`。

### 第 2 步：上传文件

`Add file` → `Upload files`，把以下文件全部拖进去：
- `monitor.py`
- `config.json`
- `.gitignore`
- `.github/`（整个文件夹）

### 第 3 步：配置发信密钥（Secrets）

`Settings` → `Secrets and variables` → `Actions` → 添加两个 Secret：

| Name | 值 |
|------|-----|
| `SMTP_USER` | 你的 QQ 邮箱（如 `2494994336@qq.com`） |
| `SMTP_PASSWORD` | QQ 邮箱授权码（16 位，去 mail.qq.com 设置里开 SMTP 获取） |

> 如果你之前的 `bianca-monitor` 项目已经配过这两个 Secret，直接用同一套，跳过这步。

### 第 4 步：填收件邮箱

打开 `config.json` 点铅笔编辑，把 `"to": ["2494994336@qq.com"]` 改成你的邮箱（默认已经填好）。

### 第 5 步：启用自动运行

`Actions` 标签页 → 若提示启用 workflow 点绿色按钮启用。

## 五、验证（发测试邮件）

1. `Actions` 页左侧点 `ndx-monitor` 工作流 → 右侧 `Run workflow` 手动触发。
2. 等 30 秒，点进去看运行日志，出现 `[邮件] 已发送` 即打通。
3. 去 QQ 邮箱收信。

收到邮件 = 取价 + 发信全链路 OK。

## 六、本地自测（可选）

```bash
python monitor.py --check   # 打印币安当前所有股票/ETF合约及涨跌幅，验证取价
python monitor.py --test    # 取价 + 发一封测试邮件
```

## 七、常见问题

**Q：需要注册 Finnhub / 币安 API key 吗？**
不需要。币安合约公开行情接口无需 key，脚本只用 Python 标准库。

**Q：币安上新合约会自动监控吗？**
会。每次运行都重新从 `exchangeInfo` 拉最新股票/ETF 合约列表。

**Q：想改阈值 / 加宽基 ETF？**
改 `config.json`：阈值在 `thresholds`，宽基 ETF 在 `index_symbols`（用 ±0.4% 阈值的那几个）。

**Q：怎么停止？**
删仓库，或 `Actions` 页 `Disable workflow`。

> 风险提示：本工具仅做价格提醒，不构成投资建议。合约交易风险极高，请自行判断。
