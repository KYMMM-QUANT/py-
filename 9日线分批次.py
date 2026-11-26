#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
菜粕加权(KQ.i@CZCE.RM) 9日线趋势反转 + 回踩确认进场策略 —— 回测版(日线)

逻辑:
  1. 条件B(反转确认): 最近4天 MA9 方向严格一致(连涨4天或连跌4天),
                      且这4天收盘价 max-min >= 15 点
  2. 条件A(原趋势):   从反转4天的前一天往前找MA9方向拐点,
                      原趋势天数 >= 12 天, 且区间收盘价 max-min >= 20 点
  3. 预备信号: 满足A+B后进入"等回踩"状态
       - 回踩判定: K线高点 >= MA9 且 低点 <= MA9 (K线包含当天MA9)
       - 有效反弹: 做多 -> 收盘价 >= MA9 - 2跳
                   做空 -> 收盘价 <= MA9 + 2跳
       - 进场价: 当天收盘价
  4. 作废规则(任一触发即作废):
       - 时限: 预备信号后超过 6 天
       - 累计: 做多有2天收盘价 < MA9 (做空相反)
  5. 平仓:
       - 止盈: 70 跳
       - 分批止损: 相对开仓价跌 30 跳平第1手, 跌 60 跳平第2手
       - 剩余持仓止盈目标不调整
       - 不补仓
  6. 资金: 初始5万, 每次开2手, 每手10吨, 手续费统一10元
"""

import datetime
import logging
from datetime import date

import pandas as pd
from tqsdk import TqApi, TqAuth, TqBacktest, BacktestFinished

logging.getLogger("tqsdk").setLevel(logging.WARNING)

# ====================== 账号 ======================
TQ_USER = "kymmm"
TQ_PWD = "syd061204LB"

# ====================== 参数 ======================
KLINE_DURATION = 60 * 60 * 24     # 日线
DATA_LENGTH = 3000
DATA_SYMBOL = "KQ.i@CZCE.RM"      # 菜粕加权(指数)合约

MA_PERIOD = 9                     # 9日线
MIN_TREND_DAYS = 12               # 条件A: 原趋势至少 12 天
TREND_REQ = 20                    # 条件A: 原趋势收盘价 max-min >= 20 点
REV_DAYS = 4                      # 条件B: 连续反向 4 天
REV_REQ = 15                      # 条件B: 4天内收盘价 max-min >= 15 点

TICK = 1                          # 最小变动价位(菜粕1点/跳)
PULLBACK_TOL = 2 * TICK           # 回踩有效反弹容忍: 收盘价距MA9不超过2跳
MAX_WAIT_DAYS = 6                 # 作废时限: 6天
MAX_BREAK_DAYS = 2                # 累计破均线天数上限

INIT_BALANCE = 50000              # 初始资金
LOTS = 2                          # 每次开仓手数
MULTIPLIER = 10                   # 每手吨数
TOTAL_FEE = 10                    # 每次开平总手续费(统一10元)

TP_POINTS = 70                    # 止盈点数
SL_POINTS_A = 30                  # 第1手止损点数
SL_POINTS_B = 60                  # 第2手止损点数

START_DATE = date(2020, 1, 1)
END_DATE = date(2026, 11, 16)


def main():
    backtest = TqBacktest(start_dt=START_DATE, end_dt=END_DATE)
    api = TqApi(backtest=backtest, auth=TqAuth(TQ_USER, TQ_PWD))
    klines = api.get_kline_serial(DATA_SYMBOL, KLINE_DURATION, DATA_LENGTH)

    balance = float(INIT_BALANCE)
    position = 0            # 0空仓, 1多, -1空
    lots_left = 0           # 剩余持仓手数
    entry_price = 0.0

    waiting_pullback = False
    wait_dir = 0
    wait_days = 0
    break_days = 0          # 累计破均线天数

    stats = {"total": 0, "win": 0, "loss": 0}
    trades = []

    def fmt(ts):
        return datetime.datetime.fromtimestamp(ts / 1e9).strftime("%Y-%m-%d")

    try:
        while True:
            api.wait_update()

            if not api.is_changing(klines.iloc[-1], "datetime"):
                continue

            if len(klines) < MA_PERIOD + MIN_TREND_DAYS + REV_DAYS + 10:
                continue

            close = klines.close
            high = klines.high
            low = klines.low
            ma9 = close.rolling(MA_PERIOD).mean()

            curr_close = float(close.iloc[-2])
            curr_high = float(high.iloc[-2])
            curr_low = float(low.iloc[-2])
            curr_ma9 = float(ma9.iloc[-2])
            curr_date = fmt(klines.datetime.iloc[-2])

            # ---------------- 1) 持仓: 分批止损 + 止盈 ----------------
            if position != 0:
                t = trades[-1]
                closed_this_bar = False

                # --- 分批止损: 第1手(亏30跳) ---
                if lots_left >= 2:
                    if position == 1 and curr_low <= entry_price - SL_POINTS_A:
                        loss = -SL_POINTS_A * 1 * MULTIPLIER
                        balance += loss
                        lots_left = 1
                        closed_this_bar = True
                        t["sl1_date"] = curr_date
                        t["sl1_price"] = round(entry_price - SL_POINTS_A, 1)
                        print(f"{curr_date} 多单第1手止损 @ {entry_price - SL_POINTS_A:.1f} "
                              f"({loss:+.0f}元) 剩余{lots_left}手", flush=True)
                    elif position == -1 and curr_high >= entry_price + SL_POINTS_A:
                        loss = -SL_POINTS_A * 1 * MULTIPLIER
                        balance += loss
                        lots_left = 1
                        closed_this_bar = True
                        t["sl1_date"] = curr_date
                        t["sl1_price"] = round(entry_price + SL_POINTS_A, 1)
                        print(f"{curr_date} 空单第1手止损 @ {entry_price + SL_POINTS_A:.1f} "
                              f"({loss:+.0f}元) 剩余{lots_left}手", flush=True)

                # --- 分批止损: 第2手(亏60跳, 全部平掉) ---
                if lots_left >= 1 and not closed_this_bar:
                    if position == 1 and curr_low <= entry_price - SL_POINTS_B:
                        loss = -SL_POINTS_B * lots_left * MULTIPLIER
                        balance += loss
                        t.update(exit_date=curr_date, exit_price=round(entry_price - SL_POINTS_B, 1),
                                 exit_reason="止损", success="失败", pnl=round(loss, 1),
                                 balance=round(balance, 1), exit_lots=lots_left)
                        print(f"{curr_date} 多单全平止损 @ {entry_price - SL_POINTS_B:.1f} ({loss:+.0f}元)", flush=True)
                        position = 0
                        lots_left = 0
                        stats["total"] += 1
                        stats["loss"] += 1
                        continue
                    elif position == -1 and curr_high >= entry_price + SL_POINTS_B:
                        loss = -SL_POINTS_B * lots_left * MULTIPLIER
                        balance += loss
                        t.update(exit_date=curr_date, exit_price=round(entry_price + SL_POINTS_B, 1),
                                 exit_reason="止损", success="失败", pnl=round(loss, 1),
                                 balance=round(balance, 1), exit_lots=lots_left)
                        print(f"{curr_date} 空单全平止损 @ {entry_price + SL_POINTS_B:.1f} ({loss:+.0f}元)", flush=True)
                        position = 0
                        lots_left = 0
                        stats["total"] += 1
                        stats["loss"] += 1
                        continue

                # --- 止盈(剩余手数, 目标不调整) ---
                if position == 1 and curr_high >= entry_price + TP_POINTS:
                    gain = TP_POINTS * lots_left * MULTIPLIER
                    balance += gain
                    t.update(exit_date=curr_date, exit_price=round(entry_price + TP_POINTS, 1),
                             exit_reason="止盈", success="成功", pnl=round(gain, 1),
                             balance=round(balance, 1), exit_lots=lots_left)
                    print(f"{curr_date} 多单止盈 @ {entry_price + TP_POINTS:.1f} "
                          f"({gain:+.0f}元, {lots_left}手)", flush=True)
                    position = 0
                    lots_left = 0
                    stats["total"] += 1
                    stats["win"] += 1
                elif position == -1 and curr_low <= entry_price - TP_POINTS:
                    gain = TP_POINTS * lots_left * MULTIPLIER
                    balance += gain
                    t.update(exit_date=curr_date, exit_price=round(entry_price - TP_POINTS, 1),
                             exit_reason="止盈", success="成功", pnl=round(gain, 1),
                             balance=round(balance, 1), exit_lots=lots_left)
                    print(f"{curr_date} 空单止盈 @ {entry_price - TP_POINTS:.1f} "
                          f"({gain:+.0f}元, {lots_left}手)", flush=True)
                    position = 0
                    lots_left = 0
                    stats["total"] += 1
                    stats["win"] += 1
                continue

            # ---------------- 2) 等待回踩 ----------------
            if waiting_pullback:
                wait_days += 1

                # 作废: 累计破均线天数
                if wait_dir == 1:
                    if curr_close < curr_ma9:
                        break_days += 1
                else:
                    if curr_close > curr_ma9:
                        break_days += 1

                if break_days >= MAX_BREAK_DAYS:
                    waiting_pullback = False
                    print(f"{curr_date} 预备信号作废(累计{break_days}天收盘破均线)", flush=True)
                    continue

                # 作废: 超时
                if wait_days > MAX_WAIT_DAYS:
                    waiting_pullback = False
                    print(f"{curr_date} 预备信号作废(等待{wait_days}天)", flush=True)
                    continue

                # 回踩判定: K线包含MA9 + 有效反弹
                bar_contains_ma = curr_high >= curr_ma9 and curr_low <= curr_ma9
                if bar_contains_ma:
                    if wait_dir == 1:
                        valid_bounce = curr_close >= curr_ma9 - PULLBACK_TOL
                    else:
                        valid_bounce = curr_close <= curr_ma9 + PULLBACK_TOL

                    if valid_bounce:
                        position = wait_dir
                        lots_left = LOTS
                        entry_price = curr_close          # 进场价 = 当天收盘价
                        waiting_pullback = False
                        trades.append({
                            "direction": "多" if position == 1 else "空",
                            "entry_date": curr_date,
                            "entry_price": round(entry_price, 1),
                            "entry_ma9": round(curr_ma9, 1),
                            "wait_days": wait_days,
                            "sl1_date": None, "sl1_price": None,
                            "exit_date": None, "exit_price": None,
                            "exit_reason": None, "success": None,
                            "exit_lots": None,
                            "pnl": None, "balance": None,
                        })
                        print(f"{curr_date} 开{('多' if position == 1 else '空')} @ {entry_price:.1f} "
                              f"(回踩收盘价, 等待{wait_days}天)", flush=True)
                continue

            # ---------------- 3) 空仓: 找信号 ----------------
            ma_recent = [float(ma9.iloc[-(REV_DAYS + 1) + i]) for i in range(REV_DAYS)]
            if any(pd.isna(v) for v in ma_recent):
                continue

            is_down = all(ma_recent[i] > ma_recent[i + 1] for i in range(REV_DAYS - 1))
            is_up = all(ma_recent[i] < ma_recent[i + 1] for i in range(REV_DAYS - 1))
            if not (is_down or is_up):
                continue

            today_dir = -1 if is_down else 1
            recent_close = close.iloc[-(REV_DAYS + 1):-1]
            if float(recent_close.max() - recent_close.min()) < REV_REQ:
                continue

            prev_dir = -today_dir
            trend_end_idx = len(ma9) - (REV_DAYS + 1)
            trend_start_idx = None
            for i in range(trend_end_idx, MA_PERIOD, -1):
                prev_val = float(ma9.iloc[i - 1])
                curr_val = float(ma9.iloc[i])
                if pd.isna(prev_val) or pd.isna(curr_val):
                    break
                if prev_dir == 1 and prev_val > curr_val:
                    trend_start_idx = i
                    break
                if prev_dir == -1 and prev_val < curr_val:
                    trend_start_idx = i
                    break

            if trend_start_idx is None:
                continue

            trend_days = trend_end_idx - trend_start_idx + 1
            if trend_days < MIN_TREND_DAYS:
                continue

            trend_close = close.iloc[trend_start_idx:trend_end_idx]
            if len(trend_close) == 0:
                continue
            if float(trend_close.max() - trend_close.min()) < TREND_REQ:
                continue

            waiting_pullback = True
            wait_dir = today_dir
            wait_days = 0
            break_days = 0
            print(f"{curr_date} 预备信号 方向={('多' if wait_dir == 1 else '空')} 原趋势{trend_days}天", flush=True)

    except BacktestFinished:
        if position != 0 and trades:
            t = trades[-1]
            last_close = float(klines.close.iloc[-2])
            float_pnl = position * (last_close - t["entry_price"]) * lots_left * MULTIPLIER
            t.update(exit_date=fmt(klines.datetime.iloc[-2]), exit_price=round(last_close, 1),
                     exit_reason="期末未平", success="未知", pnl=round(float_pnl, 1),
                     balance=round(balance + float_pnl, 1), exit_lots=lots_left)
            balance += float_pnl

        print("\n" + "=" * 50)
        print("回测结束")
        print(f"交易次数   : {len(trades)}")
        print(f"盈利/亏损  : {stats['win']} / {stats['loss']}")
        if stats["total"]:
            print(f"胜率       : {stats['win'] / stats['total'] * 100:.1f}%")
        print(f"最终权益   : {balance:.2f} 元 (初始 {INIT_BALANCE})")
        print(f"收益率     : {(balance - INIT_BALANCE) / INIT_BALANCE * 100:+.2f}%")
        print("=" * 50)

        if trades:
            tdf = pd.DataFrame(trades)
            print("\n逐笔明细:")
            print(tdf.to_string(index=False))
            tdf.to_csv("rm_pullback_v2_trades.csv", index=False, encoding="utf-8-sig")
            print("\n已导出 rm_pullback_v2_trades.csv")

            done = tdf[tdf["exit_reason"].isin(["止盈", "止损"])]
            if len(done):
                print("\n--- 多空分别 ---")
                for d in ["多", "空"]:
                    g = done[done["direction"] == d]
                    if len(g):
                        w = (g["pnl"] > 0).sum()
                        print(f"{d}: {len(g)}笔 胜率{w / len(g) * 100:.0f}% 总盈亏{g['pnl'].sum():+.0f}元")
    finally:
        api.close()


if __name__ == "__main__":
    main()