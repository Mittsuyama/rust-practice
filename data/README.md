# data/

全部是**合成数据**，由 `tools/gen_mock_data.py` 生成，仅用于练习。不要拿去做任何真实交易判断。
生成器 seed 固定（`--seed 20261120`），同一份脚本重复运行输出完全一致。

## 目录

| 路径 | 内容 | 规模 |
| --- | --- | --- |
| `raw/orders_20261120.csv` | 逐笔委托（5 只股票，2026-11-20 全天） | 12,700 行 |
| `raw/trades_20261120.csv` | 逐笔成交 | 6,402 行 |
| `expected/orderbook_600519_5s.csv` | 600519 的 5 秒快照参考答案 | 2,879 行 |
| `market/securities.csv` | 证券静态表（代码 / 名称 / 行业） | 6 行 |
| `market/bars_YYYYMMDD.csv` | 6 只股票 × 240 分钟的五档快照 | 20 个文件，每个 1,440 行 |
| `market/index_daily.csv` | 合成指数日线 | 20 行 |
| `out/` | 你程序的输出目录（已 gitignore） | — |

`bars_*` 覆盖 2026-10-26 至 2026-11-20 的工作日（20 个交易日）。
其中 `688981` 在 2026-11-10 的 10:00–10:30 人为停牌：盘口为空字符串、`volume=0`、`amount=0.00`。

字段定义、单位、语义见 [TASK1.md](../TASK1.md) 和 [TASK2.md](../TASK2.md) 的「数据」一节。

## 重新生成

```powershell
python tools/gen_mock_data.py                 # 默认 20 个交易日
python tools/gen_mock_data.py --bars-days 60  # 放大任务 2 的样本
```

生成器同时会做自检：按 TASK1.md 的规则重放 CSV，必须与内部撮合引擎的最终订单簿完全一致，
否则直接报错退出。所以数据、文档、参考答案三者是同步的。
