# rust-practice

用两个「像那么回事」的行情数据任务练 Rust 工程能力。数据已 mock 好（**合成数据，不是真实行情**），
核心代码全部由你写；我负责出题、造数据、定验收标准、做 review。

| 任务 | 主题 | 主要练什么 |
| --- | --- | --- |
| [任务 1](TASK1.md) | 逐笔委托 + 逐笔成交 → 订单簿快照 | CSV/serde、错误处理、`BTreeMap`/`VecDeque`、状态机、CLI、测试、对拍 |
| [任务 2](TASK2.md) | 多股票分钟行情 → 因子研究（开放题） | 数据清洗、统计、滚动窗口、矩阵、因子检验、简单回测 |

## 怎么用这个仓库

- **我不写实现**。每个 `TASK*.md` 里有背景、数据规范、验收标准、里程碑和提示；代码你来。
- 一次只做一个里程碑。做完先跑验收，再在 `TASK*.md` 的进度表打勾，写 3 行复盘（学到什么 / 卡在哪 / 下次注意）。
- 卡住超过 30 分钟：把 `cargo build` 报错、你的类型设计或最小复现贴给我，我们一起定位。
- 每完成一个里程碑，把 `cargo test` 的输出贴给我，我 review 完再放行下一个。
- 优先「能跑 + 有测试 + 对拍一致」，再谈性能。不要提前优化，也不要提前引大依赖。

## 环境（Windows，一次性）

```powershell
winget install --id Rustlang.Rustup -e
# 装完新开一个终端，确认：
rustc -V
cargo -V
rustup component add rustfmt clippy
```

- 你机器上已有 VS 2022 的 MSVC 工具链，rustup 默认目标 `x86_64-pc-windows-msvc` 会直接用它链接。
- winget 不可用时，从 <https://rustup.rs> 下载 `rustup-init.exe`。
- VS Code 里装 `rust-analyzer` 扩展（保存时自动 `cargo check`，比反复敲命令快）。
- 想确认工具链齐了：`cargo new --help`、`cargo clippy -V`。

## 目录

```
rust-practice/
├─ Cargo.toml            # workspace 根：集中管理依赖版本，members = crates/*
├─ rust-toolchain.toml   # 固定 stable + rustfmt/clippy
├─ crates/               # 你的 crate（现在为空，用 cargo new 创建）
│  ├─ orderbook/         # 任务 1（M0 时创建）
│  └─ quant/             # 任务 2（Q0 时创建）
├─ data/
│  ├─ raw/               # 任务 1 输入：orders_20261120.csv、trades_20261120.csv
│  ├─ expected/          # 任务 1 参考答案：orderbook_600519_5s.csv
│  ├─ market/            # 任务 2 输入：securities / bars_*.csv / index_daily
│  └─ out/               # 你的输出（已 gitignore）
├─ tools/
│  └─ gen_mock_data.py   # 数据生成器（可复现，seed 固定）
├─ TASK1.md
├─ TASK2.md
└─ data/README.md
```

## 数据速览

| 文件 | 行数 | 内容 |
| --- | --- | --- |
| `data/raw/orders_20261120.csv` | 12,700 | 逐笔委托：5 只股票、2026-11-20 全天、含撤单与市价单 |
| `data/raw/trades_20261120.csv` | 6,402 | 逐笔成交：买方/卖方委托号、成交价量、主动方 |
| `data/expected/orderbook_600519_5s.csv` | 2,879 | 600519 的 5 秒快照参考答案（逐字节对拍用） |
| `data/market/bars_YYYYMMDD.csv` | 20 × 1,440 | 6 只股票 × 240 分钟/日，含五档盘口 |
| `data/market/securities.csv` | 6 | 代码、名称、行业 |
| `data/market/index_daily.csv` | 20 | 合成指数日线 |

单位约定（整个仓库统一）：**价格/金额一律用整数「分」参与运算**，`f64` 只用于统计量；
数量单位是「股」，A 股一手 = 100 股；时间精确到毫秒。数据格式的完整字段表见各任务文档。

重新生成（Python 3.10+，seed 固定，两次跑结果一致）：

```powershell
python tools/gen_mock_data.py
```

想多造点数据（比如任务 2 扩展到 60 个交易日）：`python tools/gen_mock_data.py --bars-days 60`。

## 工程约定

- 价格/金额：`i64` 分。解析 `"1500.05"` 时不要先转 `f64`，直接按字符串拆成「元」和「分」。
- 时间：`chrono::NaiveTime`，格式串 `"%H:%M:%S%.3f"`。
- 错误：库代码用 `thiserror` 定义错误枚举；二进制入口用 `anyhow` + `.context()` 补上下文（文件名、行号）。
- 外部数据一律不要 `unwrap()`；坏数据要报「哪个文件、第几行、哪个字段」。
- 输出：UTF-8 无 BOM、LF 换行、逗号分隔、表头固定。
- 测试：模块内 `#[cfg(test)]` 写单元测试；`crates/*/tests/` 写集成测试（跑真实 CSV）。
- 提交前：`cargo fmt` + `cargo clippy --all-targets -- -D warnings` + `cargo test`。

## 当前进度

任务 1：

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| M0 | 读入两个 CSV，打印行数 / 股票列表 / 时间范围 | ☐ |
| M1 | 领域类型 + 单股票订单簿（只处理新增委托） | ☐ |
| M2 | 处理成交与撤单，订单状态机 | ☐ |
| M3 | 生成快照并对拍参考答案 | ☐ |
| M4 | 多股票 + `clap` 子命令 + 文件输出 | ☐ |
| M5 | 测试、错误注入、clippy、性能 | ☐ |

任务 2：

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| Q0 | 数据体检：行数、缺失、重复、时间连续性 | ☐ |
| Q1 | 收益、相关矩阵、行业分组 | ☐ |
| Q2 | 流动性 / 盘口因子 | ☐ |
| Q3 | 量价因子 | ☐ |
| Q4 | 因子检验：IC、分层收益 | ☐ |
| Q5 | 打分组合 + 成本 + 净值曲线 | ☐ |
| Q6 | 进阶（可选）：lead-lag / PCA / 配对 | ☐ |

## 下一步

1. 按上面的「环境」装好 Rust，确认 `cargo -V` 有输出（**这一步你自己来**，装完告诉我）。
2. 在仓库根目录：`cargo new --bin crates/orderbook`
3. 打开 [TASK1.md](TASK1.md)，从 **M0** 开始；M0 只需要你写 100 行左右的解析 + 统计。
