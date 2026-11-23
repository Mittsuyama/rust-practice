#!/usr/bin/env python3
"""Generate deterministic mock market data for the rust-practice tasks.

Everything is seeded, so running this script twice produces identical files.

Outputs
-------
data/raw/orders_20261120.csv            任务 1：逐笔委托（ADD / CANCEL，含市价单）
data/raw/trades_20261120.csv            任务 1：逐笔成交
data/expected/orderbook_600519_5s.csv   任务 1：600519 的参考答案（5 秒快照）
data/market/securities.csv              任务 2：证券静态表
data/market/bars_YYYYMMDD.csv           任务 2：分钟快照面板（6 只股票 × 20 个交易日）
data/market/index_daily.csv             任务 2：指数日线

任务 1 的模拟器是一个真实的撮合引擎：它维持订单簿、按价格-时间优先撮合，然后把
「发生了什么事」写成逐笔委托 + 逐笔成交两张表。因此这些 CSV 是自洽的：按
TASK1.md 的规则重放，能精确还原出生成时的订单簿。

用法：
    python tools/gen_mock_data.py
    python tools/gen_mock_data.py --bars-days 60 --seed 7

注：--emit 供受限环境使用，把全部文件以 JSON 输出到 stdout，不落盘。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

LOT = 100  # A 股一手 = 100 股
TICK = 1   # 最小价格变动 = 0.01 元 = 1 分

# ---------------------------------------------------------------------------
# 交易时段（毫秒，自当日 00:00:00 起）
# ---------------------------------------------------------------------------
OPEN_1 = 9 * 3_600_000 + 30 * 60_000     # 09:30:00.000
CLOSE_1 = 11 * 3_600_000 + 30 * 60_000   # 11:30:00.000
OPEN_2 = 13 * 3_600_000                  # 13:00:00.000
CLOSE_2 = 15 * 3_600_000                 # 15:00:00.000
SEGMENTS = [(OPEN_1, CLOSE_1), (OPEN_2, CLOSE_2)]
SESSION_MS = (CLOSE_1 - OPEN_1) + (CLOSE_2 - OPEN_2)

# 任务 1 的 5 只股票：(代码, 基准价(分), 事件数, 单笔最大手数)
T1_STOCKS = [
    ("600519", 150_000, 1_900, 8),
    ("601318", 5_200, 2_800, 30),
    ("600036", 3_500, 3_000, 40),
    ("601012", 1_850, 2_600, 50),
    ("688981", 8_800, 2_400, 20),
]

# 任务 2 的 6 只股票：(代码, 名称, 行业, 基准价(分), 日波动率, beta, 每分钟基准手数)
T2_STOCKS = [
    ("600519", "贵州茅台", "白酒", 150_000, 0.0130, 1.05, 8),
    ("601318", "中国平安", "保险", 5_200, 0.0160, 1.15, 90),
    ("600036", "招商银行", "银行", 3_500, 0.0140, 1.00, 120),
    ("601012", "隆基绿能", "光伏", 1_850, 0.0250, 1.30, 300),
    ("688981", "中芯国际", "半导体", 8_800, 0.0230, 1.20, 80),
    ("600276", "恒瑞医药", "医药", 4_800, 0.0170, 1.05, 60),
]

# 任务 2 中人为制造的一次停牌：10:00 - 10:30 没有盘口、没有成交
HALT = ("688981", "2026-11-10", 30, 60)  # (代码, 日期, 起始分钟序号, 结束分钟序号)

ORDER_HEADER = [
    "trade_date", "security_id", "order_time", "order_no",
    "side", "order_type", "action", "price", "qty",
]
TRADE_HEADER = [
    "trade_date", "security_id", "trade_time", "trade_no",
    "buy_order_no", "sell_order_no", "price", "qty", "aggressor_side",
]
SNAPSHOT_HEADER = ["trade_date", "security_id", "snapshot_time"]
SNAPSHOT_HEADER += [f"bid{i}_{k}" for i in range(1, 6) for k in ("price", "qty")]
SNAPSHOT_HEADER += [f"ask{i}_{k}" for i in range(1, 6) for k in ("price", "qty")]
SNAPSHOT_HEADER += ["last_price", "cum_volume", "cum_amount"]
BARS_HEADER = ["trade_date", "security_id", "bar_time", "last_price", "volume", "amount"]
BARS_HEADER += [f"bid{i}_{k}" for i in range(1, 6) for k in ("price", "qty")]
BARS_HEADER += [f"ask{i}_{k}" for i in range(1, 6) for k in ("price", "qty")]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def fmt_price(cents: int) -> str:
    """把整数分格式化成 '1500.00' 这样的字符串（不做浮点运算）。"""
    return f"{cents // 100}.{cents % 100:02d}"


def fmt_amount(fen: int) -> str:
    """整数分 -> '1234.56'（元，两位小数）。"""
    return f"{fen // 100}.{fen % 100:02d}"


def fmt_clock(ms: int) -> str:
    """当日毫秒 -> 'HH:MM:SS.mmm'。"""
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms_ = divmod(rem, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms_:03d}"


def parse_clock(text: str) -> int:
    h, m, rest = text.split(":")
    s, ms_ = rest.split(".")
    return int(h) * 3_600_000 + int(m) * 60_000 + int(s) * 1_000 + int(ms_)


def wall_ms(virtual_ms: int) -> int:
    """把 [0, SESSION_MS) 的虚拟连续时间映射到真实时钟（跳过午休）。"""
    if virtual_ms < CLOSE_1 - OPEN_1:
        return OPEN_1 + virtual_ms
    return OPEN_2 + (virtual_ms - (CLOSE_1 - OPEN_1))


class Output:
    """统一的落盘入口；--emit 模式下不落盘，最后把全部内容输出成 JSON。"""

    def __init__(self, root: Path, emit: bool):
        self.root = root
        self.emit = emit
        self.files: dict[str, str] = {}

    def write(self, path: Path, header: list[str], rows: list[tuple]) -> None:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
        text = buf.getvalue()
        if self.emit:
            self.files[path.relative_to(self.root).as_posix()] = text
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")

    def flush(self) -> None:
        if self.emit:
            json.dump({"files": self.files}, sys.stdout, ensure_ascii=True)
            sys.stdout.write("\n")


def sample_session_times(rng: random.Random, count: int) -> list[int]:
    """按 U 形强度（开盘/收盘更活跃）采样 count 个时间点，严格递增。"""
    times: list[int] = []
    while len(times) < count:
        t = rng.uniform(0.0, SESSION_MS)
        edge = 0.55 + 2.0 * (math.exp(-t / 900_000) + math.exp(-(SESSION_MS - t) / 900_000))
        if rng.random() < edge / 4.6:
            times.append(int(t))
    times.sort()
    for i in range(1, len(times)):
        if times[i] <= times[i - 1]:
            times[i] = times[i - 1] + 1
    return times


# ---------------------------------------------------------------------------
# 任务 1：撮合引擎
# ---------------------------------------------------------------------------
class Sim:
    """简单的限价订单簿撮合器，负责生成自洽的逐笔数据。"""

    def __init__(self, rng: random.Random, code: str, base: int, max_lots: int):
        self.rng = rng
        self.code = code
        self.mid = base
        self.max_lots = max_lots
        self.book: dict[str, dict[int, list]] = {"B": {}, "S": {}}
        self.orders: dict[int, list] = {}   # order_no -> [side, price, qty, type]
        self.active: dict[int, None] = {}   # 仍在簿上的订单（插入序）
        self.order_no = 0
        self.trade_no = 0
        self.last_ts = 0
        self.order_rows: list[tuple] = []
        self.trade_rows: list[tuple] = []

    # -- 簿操作 ------------------------------------------------------------
    def _rest(self, no: int, side: str, price: int, qty: int) -> None:
        self.book[side].setdefault(price, []).append([no, qty])
        self.orders[no] = [side, price, qty, "LIMIT"]
        self.active[no] = None

    def _match(self, side: str, limit: int | None, qty: int) -> tuple[int, list[tuple]]:
        opp = "S" if side == "B" else "B"
        fills: list[tuple[int, int, int]] = []
        while qty > 0 and self.book[opp]:
            best = min(self.book[opp]) if opp == "S" else max(self.book[opp])
            if limit is not None:
                if side == "B" and best > limit:
                    break
                if side == "S" and best < limit:
                    break
            level = self.book[opp][best]
            while level and qty > 0:
                maker_no, maker_qty = level[0]
                fill = min(maker_qty, qty)
                fills.append((maker_no, best, fill))
                qty -= fill
                maker_qty -= fill
                if maker_qty == 0:
                    level.pop(0)
                    del self.orders[maker_no]
                    self.active.pop(maker_no, None)
                else:
                    level[0] = [maker_no, maker_qty]
                    self.orders[maker_no][2] = maker_qty
            if not level:
                del self.book[opp][best]
        return qty, fills

    def _cancel(self, no: int, ts: int) -> None:
        side, price, qty, otype = self.orders.pop(no)
        level = self.book[side][price]
        self.book[side][price] = [o for o in level if o[0] != no]
        if not self.book[side][price]:
            del self.book[side][price]
        self.active.pop(no, None)
        self.order_rows.append((ts, no, side, otype, "C", fmt_price(price), qty))

    # -- 主循环 ------------------------------------------------------------
    def run(self, n_events: int) -> None:
        rng = self.rng
        times = sample_session_times(rng, n_events)
        for virtual in times:
            ts = max(wall_ms(virtual), self.last_ts + 1)
            self.last_ts = ts
            if rng.random() < 0.10:  # 中间价随机游走
                self.mid = max(self.mid + rng.choice((-TICK, TICK)), 100)

            r = rng.random()
            if r < 0.15 and self.active:
                no = rng.choice(tuple(self.active))
                self._cancel(no, ts)
                continue

            self.order_no += 1
            no = self.order_no
            side = "B" if rng.random() < 0.5 else "S"
            qty = LOT * rng.randint(1, self.max_lots)

            if r < 0.18:  # 市价单：只撮合，不挂簿，剩余作废
                self.order_rows.append((ts, no, side, "MARKET", "A", fmt_price(0), qty))
                _, fills = self._match(side, None, qty)
            else:
                offset = rng.choice((0, 0, 1, 1, 1, 2, 2, 3, 3, 4, 5, 6, 8, 10, 15, 20))
                if rng.random() < 0.20:
                    # 主动单：买在 mid 上方、卖在 mid 下方，会吃掉对手盘
                    price = self.mid + offset if side == "B" else self.mid - offset
                else:
                    # 被动单：买在 mid 下方、卖在 mid 上方，挂在簿上等成交
                    price = self.mid - offset if side == "B" else self.mid + offset
                price = max(price, 100)
                self.order_rows.append((ts, no, side, "LIMIT", "A", fmt_price(price), qty))
                remaining, fills = self._match(side, price, qty)
                if remaining > 0:
                    self._rest(no, side, price, remaining)

            for maker_no, trade_price, fill in fills:
                self.trade_no += 1
                buy, sell = (no, maker_no) if side == "B" else (maker_no, no)
                self.trade_rows.append(
                    (ts, self.trade_no, buy, sell, fmt_price(trade_price), fill, side)
                )

    def final_levels(self) -> dict[str, dict[int, int]]:
        levels = {"B": {}, "S": {}}
        for side in ("B", "S"):
            for price, orders in self.book[side].items():
                levels[side][price] = sum(q for _, q in orders)
        return levels


def to_order_rows(raw: list[tuple], code: str, trade_date: str) -> list[tuple]:
    out = []
    for ts, no, side, otype, action, price, qty in raw:
        out.append((trade_date, code, fmt_clock(ts), no, side, otype, action, price, qty))
    return out


def to_trade_rows(raw: list[tuple], code: str, trade_date: str) -> list[tuple]:
    out = []
    for ts, no, buy, sell, price, qty, aggressor in raw:
        out.append((trade_date, code, fmt_clock(ts), no, buy, sell, price, qty, aggressor))
    return out


# ---------------------------------------------------------------------------
# 任务 1：按 TASK1.md 的规则重放（同时充当生成器的自检和参考答案）
# ---------------------------------------------------------------------------
def replay(orders_rows: list[dict], trades_rows: list[dict], interval_ms: int) -> list[list[str]]:
    events = []
    for i, rec in enumerate(orders_rows):
        events.append((rec["ts"], 0, i, "O", rec))
    for i, rec in enumerate(trades_rows):
        events.append((rec["ts"], 1, i, "T", rec))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    levels: dict[str, dict[int, int]] = {"B": {}, "S": {}}
    orders: dict[int, dict] = {}
    cum_volume = 0
    cum_fen = 0
    last_price: int | None = None

    def apply(ev) -> None:
        nonlocal cum_volume, cum_fen, last_price
        kind, rec = ev[3], ev[4]
        if kind == "O":
            if rec["action"] == "A":
                if rec["otype"] != "LIMIT":
                    return
                orders[rec["no"]] = {"side": rec["side"], "price": rec["price"], "qty": rec["qty"]}
                levels[rec["side"]][rec["price"]] = levels[rec["side"]].get(rec["price"], 0) + rec["qty"]
            else:
                order = orders.pop(rec["no"])
                assert order["qty"] == rec["qty"], "撤单数量必须等于当时剩余量"
                lv = levels[order["side"]]
                lv[order["price"]] -= order["qty"]
                assert lv[order["price"]] >= 0
                if lv[order["price"]] == 0:
                    del lv[order["price"]]
        else:
            for no, side in ((rec["buy"], "B"), (rec["sell"], "S")):
                order = orders.get(no)
                if order is None:  # 市价单：不在簿上
                    continue
                assert order["qty"] >= rec["qty"], (no, order["qty"], rec["qty"])
                order["qty"] -= rec["qty"]
                levels[side][order["price"]] -= rec["qty"]
                if order["qty"] == 0:
                    del orders[no]
                    if levels[side][order["price"]] == 0:
                        del levels[side][order["price"]]
            cum_volume += rec["qty"]
            cum_fen += rec["price"] * rec["qty"]
            last_price = rec["price"]

    def snapshot(ts: int) -> list[str]:
        bids = sorted(levels["B"].items(), reverse=True)[:5]
        asks = sorted(levels["S"].items())[:5]
        row = [fmt_clock(ts)]
        for i in range(5):
            row += [fmt_price(bids[i][0]), bids[i][1]] if i < len(bids) else ["", ""]
        for i in range(5):
            row += [fmt_price(asks[i][0]), asks[i][1]] if i < len(asks) else ["", ""]
        row += [
            fmt_price(last_price) if last_price is not None else "",
            cum_volume,
            fmt_amount(cum_fen),
        ]
        return row

    rows: list[list[str]] = []
    idx = 0
    first_ts = events[0][0]
    last_ts = events[-1][0]
    for seg_start, seg_end in SEGMENTS:
        start = max(seg_start, first_ts)
        if start > last_ts or start >= seg_end:
            continue
        t = -(-start // interval_ms) * interval_ms
        while t < seg_end and t <= last_ts:
            while idx < len(events) and events[idx][0] <= t:
                apply(events[idx])
                idx += 1
            rows.append(snapshot(t))
            t += interval_ms
    return rows


def replay_final(orders_rows: list[dict], trades_rows: list[dict]) -> dict[str, dict[int, int]]:
    """只重放不收快照，返回最终订单簿（用于自检）。"""
    events = [(r["ts"], 0, i, "O", r) for i, r in enumerate(orders_rows)]
    events += [(r["ts"], 1, i, "T", r) for i, r in enumerate(trades_rows)]
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    levels: dict[str, dict[int, int]] = {"B": {}, "S": {}}
    orders: dict[int, dict] = {}
    for ev in events:
        kind, rec = ev[3], ev[4]
        if kind == "O":
            if rec["action"] == "A":
                if rec["otype"] != "LIMIT":
                    continue
                orders[rec["no"]] = {"side": rec["side"], "price": rec["price"], "qty": rec["qty"]}
                levels[rec["side"]][rec["price"]] = levels[rec["side"]].get(rec["price"], 0) + rec["qty"]
            else:
                order = orders.pop(rec["no"])
                lv = levels[order["side"]]
                lv[order["price"]] -= order["qty"]
                if lv[order["price"]] == 0:
                    del lv[order["price"]]
        else:
            for no, side in ((rec["buy"], "B"), (rec["sell"], "S")):
                order = orders.get(no)
                if order is None:
                    continue
                order["qty"] -= rec["qty"]
                levels[side][order["price"]] -= rec["qty"]
                if order["qty"] == 0:
                    del orders[no]
                    if levels[side][order["price"]] == 0:
                        del levels[side][order["price"]]
    return levels


# ---------------------------------------------------------------------------
# 任务 2：分钟面板
# ---------------------------------------------------------------------------
def weekday_dates(start: date, count: int) -> list[str]:
    out: list[str] = []
    d = start
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def bar_label(minute: int) -> str:
    """分钟序号 -> bar 起始时间 'HH:MM:SS'。前 120 分钟是上午盘，之后是下午盘。"""
    base = OPEN_1 + (minute * 60_000 if minute < 120 else (OPEN_2 - CLOSE_1) + minute * 60_000)
    return fmt_clock(base)[:8]


def u_shape(minute: int) -> float:
    x = minute / 239.0
    return 0.6 + 1.6 * (math.exp(-x / 0.05) + math.exp(-(1 - x) / 0.05))


def gen_market(out: Output, root: Path, rng: random.Random, days: int, start: date) -> None:
    market_dir = root / "data" / "market"
    dates = weekday_dates(start, days)

    out.write(
        market_dir / "securities.csv",
        ["security_id", "name", "industry"],
        [(code, name, industry) for code, name, industry, *_ in T2_STOCKS],
    )

    prices = {code: base for code, _, _, base, *_ in T2_STOCKS}
    index_close = 3800.0
    index_rows = []

    for day in dates:
        # 当日市场共同因子：每分钟一个收益率
        minute_sigma = 0.008 / math.sqrt(240)
        market_min = [rng.gauss(0.0, minute_sigma) for _ in range(240)]
        day_factor = math.exp(sum(market_min)) - 1.0

        index_rows.append((day, f"{index_close:.2f}", f"{day_factor * 100:.2f}"))
        index_close *= 1.0 + day_factor

        rows_by_stock: list[tuple] = []
        for code, _name, _industry, _base, daily_vol, beta, base_lots in T2_STOCKS:
            sigma_min = daily_vol / math.sqrt(240)
            price = prices[code]
            for minute in range(240):
                ret = beta * market_min[minute] + rng.gauss(0.0, sigma_min)
                price = max(TICK, round(price * math.exp(ret)))
                prices[code] = price

                half = rng.choice((1, 1, 2, 2, 3, 4))
                bid = max(TICK, price - half)
                ask = price + half

                halted = code == HALT[0] and day == HALT[1] and HALT[2] <= minute < HALT[3]
                if halted:
                    bids = [("", "")] * 5
                    asks = [("", "")] * 5
                    volume = 0
                    amount = "0.00"
                else:
                    bids = []
                    asks = []
                    for level in range(5):
                        bq = LOT * max(1, round(base_lots * math.exp(-0.35 * level)
                                                * math.exp(rng.gauss(0.0, 0.4))))
                        aq = LOT * max(1, round(base_lots * math.exp(-0.35 * level)
                                                * math.exp(rng.gauss(0.0, 0.4))))
                        bids.append((fmt_price(max(TICK, bid - level * TICK)), bq))
                        asks.append((fmt_price(ask + level * TICK), aq))

                    shape = u_shape(minute)
                    intensity = 0.7 + 1.6 * min(abs(ret) / sigma_min, 6.0)
                    volume = LOT * max(1, round(base_lots * shape * intensity
                                                * math.exp(rng.gauss(0.0, 0.45))))
                    amount = fmt_amount(price * volume)

                row = (day, code, bar_label(minute), fmt_price(price), volume, amount)
                for p, q in bids:
                    row += (p, q)
                for p, q in asks:
                    row += (p, q)
                rows_by_stock.append(row)

        rows_by_stock.sort(key=lambda r: (r[1], r[2]))
        out.write(market_dir / f"bars_{day.replace('-', '')}.csv", BARS_HEADER, rows_by_stock)

    out.write(market_dir / "index_daily.csv", ["trade_date", "close", "pct_chg"], index_rows)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--seed", type=int, default=20261120)
    parser.add_argument("--date", default="2026-11-20", help="任务 1 的交易日")
    parser.add_argument("--interval-ms", type=int, default=5000, help="参考答案的快照间隔")
    parser.add_argument("--reference-symbol", default="600519", help="生成参考答案的股票")
    parser.add_argument("--bars-days", type=int, default=20, help="任务 2 的交易日数")
    parser.add_argument("--bars-start", default="2026-10-26", help="任务 2 的起始日期")
    parser.add_argument("--emit", action="store_true", help="把文件以 JSON 输出到 stdout（不落盘）")
    args = parser.parse_args()

    root: Path = args.root.resolve()
    out = Output(root, args.emit)
    rng = random.Random(args.seed)

    # -- 任务 1 ----------------------------------------------------------
    all_orders: list[tuple] = []
    all_trades: list[tuple] = []
    parsed_orders: dict[str, list[dict]] = {}
    parsed_trades: dict[str, list[dict]] = {}
    sims: dict[str, Sim] = {}

    for code, base, n_events, max_lots in T1_STOCKS:
        sim = Sim(rng, code, base, max_lots)
        sim.run(n_events)
        sims[code] = sim
        all_orders.extend(to_order_rows(sim.order_rows, code, args.date))
        all_trades.extend(to_trade_rows(sim.trade_rows, code, args.date))

    all_orders.sort(key=lambda r: (r[2], r[1]))
    all_trades.sort(key=lambda r: (r[2], r[1]))
    out.write(root / "data" / "raw" / f"orders_{args.date.replace('-', '')}.csv",
              ORDER_HEADER, all_orders)
    out.write(root / "data" / "raw" / f"trades_{args.date.replace('-', '')}.csv",
              TRADE_HEADER, all_trades)

    # 解析回字典，重放并自检（确保 TASK1.md 的规则真的能还原订单簿）
    for row in all_orders:
        parsed_orders.setdefault(row[1], []).append({
            "ts": parse_clock(row[2]), "no": int(row[3]), "side": row[4],
            "otype": row[5], "action": row[6],
            "price": round(float(row[7]) * 100), "qty": int(row[8]),
        })
    for row in all_trades:
        parsed_trades.setdefault(row[1], []).append({
            "ts": parse_clock(row[2]), "buy": int(row[4]), "sell": int(row[5]),
            "price": round(float(row[6]) * 100), "qty": int(row[7]),
        })

    expected_rows: list[list[str]] = []
    for code in parsed_orders:
        snap = replay(parsed_orders[code], parsed_trades.get(code, []), args.interval_ms)
        assert snap, f"{code} 没有生成任何快照"
        assert replay_final(parsed_orders[code], parsed_trades.get(code, [])) == sims[code].final_levels(), \
            f"{code} 重放结果与撮合引擎不一致"
        if code == args.reference_symbol:
            expected_rows = [[args.date, code] + r for r in snap]

    out.write(root / "data" / "expected" / f"orderbook_{args.reference_symbol}_5s.csv",
              SNAPSHOT_HEADER, expected_rows)

    # -- 任务 2 ----------------------------------------------------------
    gen_market(out, root, rng, args.bars_days, date.fromisoformat(args.bars_start))
    out.flush()

    print(f"orders : {len(all_orders):>6} rows", file=sys.stderr)
    print(f"trades : {len(all_trades):>6} rows", file=sys.stderr)
    print(f"expect : {len(expected_rows):>6} rows", file=sys.stderr)
    print(f"bars   : {args.bars_days * len(T2_STOCKS) * 240:>6} rows", file=sys.stderr)


if __name__ == "__main__":
    main()
