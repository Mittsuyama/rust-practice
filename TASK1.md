# 任务 1：从逐笔委托 / 逐笔成交重建订单簿

> 交付物：一个 `orderbook` CLI，把一天的 Level-2 逐笔数据重放成 5 档盘口快照 CSV。
> 状态：⬜ 未开始 · 预计 6 个里程碑（M0–M5）· 参考实现不给，只给规范和提示

这个任务练的是「工程」而不是「算法」：读文件、解析、维护状态、输出、对拍、报错。
真实交易所的行情网关、回放系统、撮合引擎都在做同一件事，只是规模大几个数量级。

---

## 1. 背景：订单簿是什么

限价订单簿（limit order book）就是「所有还没成交的买单和卖单」按价格排好的两张表：

```
        卖盘（asks）              买盘（bids）
  1500.05 ×  700  ← 卖一       1500.04 ×  500  ← 买一
  1500.06 × 1200               1500.03 ×  900
  1500.07 ×  300               1500.02 × 1500
   ...                          ...
```

- 买单价格越高、卖单价格越低，越容易成交；成交按**价格优先、时间优先**：
  最好的价格先成交，同价位先挂的单先成交（FIFO）。
- 买一（best bid）和卖一（best ask）之间的差叫**买卖价差**（spread）；两边数量之和是**深度**。
- 你看到的「盘口五档」是某一瞬间订单簿的**快照**；交易所实际推送的是**逐笔事件流**：
  谁挂了单、谁撤了单、谁和谁成交了。快照是事件流重放出来的结果。

所以任务的核心就一句话：**按时间顺序重放事件，维护订单簿，在指定时刻把前 5 档导出来。**

概念补充（各 5 分钟）：

- 订单簿：<https://en.wikipedia.org/wiki/Order_book>
- 价格-时间优先：<https://en.wikipedia.org/wiki/Order_(exchange)#Price-time_priority>

## 2. 输入数据

两份 CSV，UTF-8、LF、逗号分隔、首行表头，覆盖 2026-11-20 一个交易日、5 只股票：

| 文件 | 行数 | 含义 |
| --- | --- | --- |
| `data/raw/orders_20261120.csv` | 12,700 | 逐笔委托：新增（A）与撤单（C） |
| `data/raw/trades_20261120.csv` | 6,402 | 逐笔成交：每笔成交对应买方/卖方两个委托号 |

真实的上交所 Level-2 逐笔接口字段更多（订单号、交易阶段、撤单标志、成交方向、原始订单类型等），
这里只保留重建订单簿所必需的最小集合；schema 设计参考了公开接口文档：

- 上证所信息网络 Level-2 文档：<https://www.sseinfo.com/services/assortment/document/develop/c/10759167/files/1ff4c6d4ef314ce985d10fefe7b15f77.pdf>
- 上交所技术接口说明：<https://www.sse.org.cn/marketServices/technicalservice/interface/P020241016554547529134.pdf>

### 2.1 orders.csv

| 列 | 类型 | 示例 | 说明 |
| --- | --- | --- | --- |
| `trade_date` | date | `2026-11-20` | 交易日 |
| `security_id` | string | `600519` | 6 位代码 |
| `order_time` | `HH:MM:SS.mmm` | `09:30:01.032` | 毫秒精度 |
| `order_no` | u64 | `1` | **每只股票、每个交易日**内唯一且递增；撤单行复用它 |
| `side` | `B` / `S` | `B` | 买 / 卖 |
| `order_type` | `LIMIT` / `MARKET` | `LIMIT` | 市价单的 `price` 恒为 `0.00` |
| `action` | `A` / `C` | `A` | 新增 / 撤单 |
| `price` | 元，两位小数 | `1500.05` | 限价单的委托价 |
| `qty` | 股 | `2300` | 100 的整数倍；撤单行 = 这次撤掉的剩余量 |

样例（含新增、撤单、市价单）：

```csv
2026-11-20,600036,09:30:01.032,1,B,LIMIT,A,35.04,2300
2026-11-20,601012,09:30:00.416,1,S,MARKET,A,0.00,4400
2026-11-20,600036,09:30:08.148,2,B,LIMIT,C,34.98,700
```

### 2.2 trades.csv

| 列 | 类型 | 示例 | 说明 |
| --- | --- | --- | --- |
| `trade_date` | date | `2026-11-20` | |
| `security_id` | string | `600519` | |
| `trade_time` | `HH:MM:SS.mmm` | `09:30:34.237` | 与主动方委托同一毫秒 |
| `trade_no` | u64 | `1` | 每只股票当日递增 |
| `buy_order_no` | u64 | `13` | 买方委托号 |
| `sell_order_no` | u64 | `5` | 卖方委托号 |
| `price` | 元 | `1500.00` | 成交价 = 被动方（挂单方）的价格 |
| `qty` | 股 | `200` | 成交量 |
| `aggressor_side` | `B` / `S` | `B` | 主动方方向（分析用，重建时用不到） |

```csv
2026-11-20,600519,09:30:34.237,1,13,5,1500.00,200,B
```

> 这两份数据是用一个真实撮合引擎跑出来的：逐笔事件和最终订单簿完全自洽。
> 生成器 `tools/gen_mock_data.py` 每次生成完都会按第 3 节的规则重放一遍，
> 重放结果与引擎内部状态不一致就直接报错。

## 3. 重放语义（最关键的一节，实现前先读三遍）

### 3.1 事件的全序

把两个文件的事件合并成一条时间线：

1. 按时间升序（`order_time` / `trade_time`，都换算成当日毫秒）。
2. **同一毫秒**内：先处理 `orders.csv` 的行，再处理 `trades.csv` 的行。
3. `orders.csv` 内部按文件出现顺序处理（本数据集同一股票同一毫秒最多一条委托事件，
   但你的代码不要依赖这个假设）。

为什么是这个顺序：主动单的「新增委托」和它产生的「成交」在同一毫秒。
先挂、后扣，才能得到正确结果。

### 3.2 四种事件

| 事件 | 处理 |
| --- | --- |
| `action=A, order_type=LIMIT` | 把 **qty 全量**挂到 `price` 价位队尾。**不要自己撮合**。 |
| `action=A, order_type=MARKET` | 不进入订单簿。它只会在同一毫秒的成交里出现；未成交部分视为撤销。 |
| `action=C` | 把该 `order_no` 的**全部剩余量**移出订单簿。行里的 `qty` 应等于当时剩余量，可以用来断言。 |
| `trades` 行 | 对 `buy_order_no` 和 `sell_order_no` 各扣减 `qty`；不在簿上的（市价单）跳过。 |

关键点：委托的 `qty` 是**原始委托量**，成交由 trades 行单独给出。先全量挂、再由成交扣，
等价于交易所「撮合后的结果」。如果你在挂单时自己撮合一遍，就会把同一笔成交扣两次。

### 3.3 一个可以拿来调试的小例子

```
09:30:00.100  ADD  B LIMIT 10.00 x 300   -> 买一 10.00 300
09:30:00.100  ADD  S LIMIT 10.02 x 200   -> 卖一 10.02 200
09:30:00.500  ADD  S LIMIT 10.00 x 500   -> 先挂到卖 10.00（此刻簿是交叉的，正常）
09:30:00.500  TRADE  buy=1 sell=3 10.00 x 300   -> 买方 1 剩 0（成交完），卖方 3 剩 200
```

同一毫秒内先 ADD 再 TRADE，扣完以后 `卖一 10.00 x 200`、`买一 10.00 x 0` 被删掉，
最终簿恢复不交叉。快照只会在这些事件都处理完之后生成，所以永远不会看到交叉盘口。

### 3.4 订单状态

建议在实现里维护每笔订单的状态（`New / PartiallyFilled / Filled / Cancelled`），
成交时递减剩余量，剩余为 0 就移出订单簿；撤单时把剩余量移出。

## 4. 输出规范

单文件 `orderbook_<date>.csv`，UTF-8、LF、逗号分隔：

```
trade_date,security_id,snapshot_time,bid1_price,bid1_qty,...,bid5_price,bid5_qty,
ask1_price,ask1_qty,...,ask5_price,ask5_qty,last_price,cum_volume,cum_amount
```

共 26 列，顺序固定。含义：

| 列 | 说明 |
| --- | --- |
| `snapshot_time` | `HH:MM:SS.mmm` |
| `bid1_price` … `bid5_qty` | 买盘第 1–5 档，价格（元）/ 数量（股）交替 |
| `ask1_price` … `ask5_qty` | 卖盘第 1–5 档 |
| `last_price` | 截至该快照的最后一笔成交价；还没有成交时留空 |
| `cum_volume` | 截至该快照的累计成交量（股） |
| `cum_amount` | 截至该快照的累计成交额（元，两位小数） |

**空值约定**：某一侧不足 5 档时，缺的档位 `price` 和 `qty` **都写空字符串**（不要写 `0`）。
`cum_volume` / `cum_amount` 永远有值（可以是 `0` / `0.00`）。

### 4.1 快照时刻怎么取

参数 `I` = 快照间隔（默认 5000 ms）。记该股票第一条事件时间为 `t_first`、最后一条为 `t_last`。

对每个连续竞价时段 `[s, e)` —— 上午 `09:30:00–11:30:00`，下午 `13:00:00–15:00:00`：

> 取所有满足 `T ≥ max(s, t_first)`、`T < e`、`T ≤ t_last`、且 `T` 是当日 00:00:00 起
> `I` 整数倍（毫秒）的时刻，按升序输出。

每个快照包含**时间戳 ≤ T 的全部事件**处理完之后的订单簿。

例：`t_first = 09:30:00.312`，`I = 5000` → 第一个 `T = 09:30:05.000`（09:30:00.000 早于首个事件）；
`t_last = 14:59:58.100` → 下午最后一个 `T = 14:59:55.000`。午休期间不输出快照。

### 4.2 行序

整个文件按 `(snapshot_time, security_id)` 升序。只有一个股票时就是按时间升序。

### 4.3 参考样例

`data/expected/orderbook_600519_5s.csv` 是 `600519`、`I = 5000` 的完整参考答案（2,879 行）。
M3 的对拍就是和它逐字节比较。

## 5. 验收标准

M3 完成后，下面的命令应当**没有任何输出**（Windows 的 `fc` 会打印 "FC: no differences encountered"）：

```powershell
cargo run --release -p orderbook -- snapshots `
  --orders data/raw/orders_20261120.csv `
  --trades data/raw/trades_20261120.csv `
  --interval-ms 5000 --security 600519 `
  --out data/out/orderbook_600519.csv

fc.exe data\out\orderbook_600519.csv data\expected\orderbook_600519_5s.csv
```

- [ ] M0：两个文件的行数、股票列表、时间范围都能打印，坏文件能报出文件名和行号
- [ ] M2：跑完一天不 panic；`cum_volume` 的末值 == `trades.csv` 里该股票 `qty` 之和
- [ ] M3：与参考答案逐字节一致
- [ ] M4：5 只股票一起跑，输出行数 = 各股票快照数之和，且按 `(snapshot_time, security_id)` 有序
- [ ] 单元测试覆盖：价格单位、空档位、撤单、市价单、同毫秒顺序、午休边界
- [ ] `cargo clippy --all-targets -- -D warnings` 通过（M5）

## 6. 里程碑

每个里程碑都有「完成标志」。做不出来时先看第 8 节「常见坑」，再找我。

### M0 · 读入与体检（半天）

**你会练到**：`cargo new`、workspace 依赖继承、`serde` derive、`csv` crate、`anyhow::Context`、`NaiveTime` 解析。

- `cargo new --bin crates/orderbook`，在它的 `Cargo.toml` 里加
  `csv.workspace = true`、`serde.workspace = true`、`chrono.workspace = true`、`anyhow.workspace = true`。
- 定义 `OrderRow` / `TradeRow`（字段名与表头一致，`Deserialize` 直接可用）。
- 时间先按 `String` 收进来，用 `NaiveTime::parse_from_str(s, "%H:%M:%S%.3f")` 转。
- `ReaderBuilder::new().from_path(path)` + `reader.deserialize()`；出错时用 `reader.position()` 拿到行号，
  用 `.with_context(|| format!("解析 {path:?} 第 {} 行", pos.line()))` 补上下文。
- 实现一个 `stats` 子命令：打印每个文件的行数、股票代码列表、最早/最晚时间。

**完成标志**：`cargo run -q -- stats` 输出 `orders: 12700 rows`、`trades: 6402 rows`、5 个代码。
（行数必须是这两个数；时间范围打印出来即可。）

### M1 · 领域类型 + 单股票订单簿（半天到一天）

**你会练到**：newtype、枚举、`BTreeMap` 的 `range`、`VecDeque`、`Entry` API、函数的可变借用。

- 用整数分表示价格；`Side` 枚举；`OrderNo = u64`。
- 订单簿：`BTreeMap<Price, VecDeque<OrderNo>>` 管档位和队列，`HashMap<OrderNo, Order>` 管订单剩余量。
- 先**只处理 `action=A && order_type=LIMIT`**，把 600519 的簿建起来，写个 `top5()` 打印。
- 取最优价：买单用 `bids.range(..).next_back()`，卖单 `asks.range(..).next()`。

**完成标志**：打印 600519 在 `09:35:00`、`10:00:00`、`14:00:00` 的三个时刻的五档。
此时还没扣成交，数量会偏大——正常，M2 修。

### M2 · 成交、撤单、订单状态（一天）

**你会练到**：状态机、`Option` 与缺失、错误传播（返回 `Result` 而不是 `panic`）、不变量断言。

- 成交：双边递减；找不到订单就跳过（市价单）。递减到 0 时从档位队列里摘除，档位空了要删 key。
- 撤单：档案里取剩余量，与行里的 `qty` 比对，不一致就返回错误（`thiserror` 定义一个 `BadData`）。
- 市价单：不挂簿。
- 实现 `cum_volume` / `cum_amount` / `last_price`（用整数分累加，最后格式化）。
- 建议把「应用一个事件」写成 `Book::apply(&mut self, ev: &Event) -> Result<()>`，主循环只管排序和调用。

**完成标志**：跑完 600519 全天，最后 `cum_volume == 231100`（这是参考答案里最后一行的值）；
所有撤单校验通过；人为删掉一行、改一个数字时能报错而不是 panic。

### M3 · 快照 + 对拍（半天）

**你会练到**：时间桶、稳定排序、`csv::Writer`、集成测试、对拍调试。

- 按 4.1 的公式生成 `T`，注意三个边界：首个事件、午休、最后一条事件。
- 事件排序 key：`(ts_ms, 0 for order / 1 for trade, 文件内序号)`。
- 快照取 `ts <= T` 的累计成交，档位按价格排序取前 5。
- 写 CSV 时把空档位写成空字符串；`cum_amount` 用整数分转 `"1234.56"`。
- 跑 600519，和 `data/expected/orderbook_600519_5s.csv` 对拍。

**完成标志**：`fc.exe` 报告无差异。

调试建议：找第一处不同的行，从那个 `T` 往前推 5 秒，把相关事件按时间打印出来手算。

### M4 · 多股票 + CLI（半天）

**你会练到**：`clap` derive、子命令、参数校验、按 `(time, symbol)` 归并排序。

- `orderbook snapshots --orders ... --trades ... --interval-ms 5000 [--security 600519] --out ...`
- 每只股票独立维护订单簿，最后把所有行按 `(snapshot_time, security_id)` 排序写出。
- 校验参数：间隔必须 > 0；`--security` 必须存在；输出目录不存在时创建。

**完成标志**：全量 5 只股票跑通，输出行数等于各股票快照数之和，时间严格不减。

### M5 · 工程质量（一天，可选但推荐）

- `cargo fmt` / `cargo clippy --all-targets -- -D warnings` 清零。
- 集成测试 `crates/orderbook/tests/`：跑真实 CSV，断言末行 `cum_volume`、断言无交叉盘口、断言行数。
- 错误注入：复制一份 CSV，删一列 / 把 `qty` 改成非数字 / 重复 `order_no`，确认报错信息带行号。
- 性能：把 5 只股票的数据复制 10 份拼成一个大文件，`--release` 跑一遍，记下耗时和峰值内存。
  瓶颈大概率在字符串解析和 `HashMap`，而不是算法。

## 7. 类型设计建议（只给形状，不给实现）

```rust
pub type Price = i64;   // 单位：分
pub type Qty = i64;     // 单位：股
pub type OrderNo = u64;

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Side { Buy, Sell }

pub struct Order {
    pub no: OrderNo,
    pub side: Side,
    pub price: Price,
    pub remaining: Qty,
}

pub struct Book {
    bids: BTreeMap<Price, VecDeque<OrderNo>>,  // 高价在前，用 range(..).next_back()
    asks: BTreeMap<Price, VecDeque<OrderNo>>,
    orders: HashMap<OrderNo, Order>,
}

impl Book {
    pub fn apply_add(&mut self, ...) -> Result<()>;
    pub fn apply_trade(&mut self, ...) -> Result<()>;
    pub fn apply_cancel(&mut self, ...) -> Result<()>;
    pub fn top5(&self) -> (Vec<(Price, Qty)>, Vec<(Price, Qty)>);
}
```

如果想再进一步：把 `Price` 做成 newtype（`struct Price(i64)`）并实现 `Ord`，
让编译器帮你挡住「拿数量当价格」这类错。

## 8. 常见坑

1. **用 `f64` 存价格**：`0.1 + 0.2 != 0.3`，档位会错位。解析 `"1500.05"` 时按字符串拆成 `150005` 分。
2. **`BTreeMap` 用 `f64` 做 key**：`f64` 没有 `Ord`，编译不过；就算用 `OrderedFloat` 也建议整数分。
3. **自己撮合**：ADD 时又匹配一次，成交被扣两遍。ADD 只挂单。
4. **市价单挂到 0.00 价位**：必须跳过。
5. **撤单后档位没删**：输出里出现 `qty=0` 的档位，与参考答案不一致。
6. **同毫秒顺序**：先 trades 后 orders 会得到「负剩余量」。
7. **空档位写 0**：参考答案里是空字符串。`Option` 别用 `0` 代替。
8. **午休也生成快照**：11:30–13:00 没有事件，会多出上千行重复数据。
9. **`HashMap` 迭代顺序**：订单表不能直接拿来输出，必须先排序。
10. **CSV 的 BOM/CRLF**：Excel 另存的 CSV 带 BOM，表头会变成 `\u{feff}trade_date`。
    `csv` crate 不会自动去 BOM，需要自己处理（读第一行或 `strip_prefix('\u{feff}')`）。

## 9. 思考题（做完 M5 再想）

1. 交易所为什么不直接推快照，而要推逐笔事件？
2. 如果数据有丢包（少了一条 ADD 或 TRADE），你怎么发现？需要什么额外字段才能修复？
3. 集合竞价（09:15–09:25、14:57–15:00）的订单簿和连续竞价有什么不同？开盘价怎么产生？
4. 队列位置对做市商意味着什么？在你现在的结构里，怎么输出「某笔订单前面还有多少量」？
5. 单线程下 100 万行/秒的瓶颈会在哪里？如果要多线程，按什么维度切分最自然？
6. 同一毫秒内的事件顺序会影响结果吗？构造一个反例。

## 10. 进度与复盘

| 里程碑 | 状态 | 完成日期 | 3 行复盘 |
| --- | --- | --- | --- |
| M0 | ☐ | | |
| M1 | ☐ | | |
| M2 | ☐ | | |
| M3 | ☐ | | |
| M4 | ☐ | | |
| M5 | ☐ | | |

> 复盘写什么：卡住的具体位置（类型/借用/逻辑）、怎么解决的、下次怎么避免。
> 这些比「做完了」有用得多，三个月后回来看就靠它。
