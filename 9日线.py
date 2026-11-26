#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
菜粕加权(KQ.i@CZCE.RM) 9日线趋势反转 + 回踩进场策略 —— 回测版

逻辑:
  1. 条件B(反转确认): 最近4天 MA9 方向严格一致(连涨4天或连跌4天),
                      且这4天收盘价 max-min >= 20 点
  2. 条件A(原趋势):   从反转4天的前一天往前找MA9方向拐点,
                      原趋势天数 >= 12 天, 且区间收盘价 max-min >= 40 点
  3. 开仓: 满足A+B后不立刻开, 进入"等回踩"状态(最多5天):
           做多 -> 某天 low <= MA9 时, 以 MA9 为成交价买入
           做空 -> 某天 high >= MA9 时, 以 MA9 为成交价卖出
  4. 平仓: 固定止盈80点 / 止损50点, 不看均线
  5. 资金: 初始5万, 每次2手, 每手10吨, 手续费统一10元
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
TREND_REQ = 40                    # 条件A: 原趋势收盘价 max-min >= 40 点
REV_DAYS = 4                      # 条件B: 连续反向 4 天
REV_REQ = 20                      # 条件B: 4天内收盘价 max-min >= 20 点

PULLBACK_OFFSET = 0               # 回踩基准: MA9 ± 0 (即恰好回到MA9)
MAX_WAIT_DAYS = 5                 # 最多等 5 天回踩

INIT_BALANCE = 50000              # 初始资金
LOTS = 2                          # 每次开仓手数
MULTIPLIER = 10                   # 每手吨数
TOTAL_FEE = 10                    # 每次开平总手续费(统一10元)
TP_POINTS = 80                    # 止盈点数
SL_POINTS = 50                    # 止损点数

START_DATE = date(2020, 1, 1)
END_DATE = date(2026, 11, 16)


def main():
    backtest = TqBacktest(start_dt=START_DATE, end_dt=END_DATE)
    api = TqApi(backtest=backtest, auth=TqAuth(TQ_USER, TQ_PWD))
    klines = api.get_kline_serial(DATA_SYMBOL, KLINE_DURATION, DATA_LENGTH)

    balance = float(INIT_BALANCE)
    position = 0            # 0空仓, 1多, -1空
    entry_price = 0.0
    entry_date = ""

    waiting_pullback = False
    wait_dir = 0
    wait_days = 0

    stats = {"total": 0, "win": 0, "loss": 0}

    def fmt(ts):
        return datetime.datetime.fromtimestamp(ts / 1e9).strftime("%Y-%m-%d")

    try:
        while True:
            api.wait_update()

            # 每来一根新K线才处理(tick级变化直接跳过)
            if not api.is_changing(klines.iloc[-1], "datetime"):
                continue

            # iloc[-2] = 最近一根已完成K线(收盘价可靠)
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

            # ---------------- 1) 持仓: 只检查止盈止损 ----------------
            if position != 0:
                reason, pnl, exit_px = None, 0.0, 0.0

                if position == 1:
                    tp_px = entry_price + TP_POINTS
                    sl_px = entry_price - SL_POINTS
                    # 同一根K线同时触及: 保守起见按"先止损"处理
                    if curr_low <= sl_px:
                        reason, pnl, exit_px = "止损", -SL_POINTS * LOTS * MULTIPLIER - TOTAL_FEE, sl_px
                    elif curr_high >= tp_px:
                        reason, pnl, exit_px = "止盈", TP_POINTS * LOTS * MULTIPLIER - TOTAL_FEE, tp_px
                else:
                    tp_px = entry_price - TP_POINTS
                    sl_px = entry_price + SL_POINTS
                    if curr_high >= sl_px:
                        reason, pnl, exit_px = "止损", -SL_POINTS * LOTS * MULTIPLIER - TOTAL_FEE, sl_px
                    elif curr_low <= tp_px:
                        reason, pnl, exit_px = "止盈", TP_POINTS * LOTS * MULTIPLIER - TOTAL_FEE, tp_px

                if reason:
                    balance += pnl
                    stats["total"] += 1
                    stats["win" if pnl > 0 else "loss"] += 1
                    print(f"{curr_date} 平{('多' if position == 1 else '空')} @ {exit_px:.1f} "
                          f"[{reason}] {pnl:+.0f} 元  权益={balance:.0f}", flush=True)
                    position = 0
                continue

            # ---------------- 2) 等待回踩 ----------------
            if waiting_pullback:
                wait_days += 1
                hit = False
                entry_px = 0.0

                if wait_dir == 1:
                    # 做多: 回踩到 MA9 -> 以 MA9 为成交价
                    pullback_px = curr_ma9 + PULLBACK_OFFSET
                    if curr_low <= pullback_px:
                        hit = True
                        entry_px = pullback_px
                else:
                    # 做空: 反抽到 MA9 -> 以 MA9 为成交价
                    pullback_px = curr_ma9 - PULLBACK_OFFSET
                    if curr_high >= pullback_px:
                        hit = True
                        entry_px = pullback_px

                if hit:
                    position = wait_dir
                    entry_price = entry_px
                    entry_date = curr_date
                    waiting_pullback = False
                    print(f"{curr_date} 开{('多' if position == 1 else '空')} @ {entry_price:.1f} (MA9基准回踩进场)", flush=True)
                elif wait_days >= MAX_WAIT_DAYS:
                    waiting_pullback = False
                    print(f"{curr_date} 信号作废(等待{MAX_WAIT_DAYS}天未回踩)", flush=True)
                continue

            # ---------------- 3) 空仓: 找信号 ----------------
            # 条件B: 最近 REV_DAYS 根已完成K线的MA9方向
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

            # 条件A: 反转段之前的原趋势
            prev_dir = -today_dir
            trend_end_idx = len(ma9) - (REV_DAYS + 1)   # 反转段前一位置
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

            # A+B 同时满足 -> 进入等待回踩
            waiting_pullback = True
            wait_dir = today_dir
            wait_days = 0
            print(f"{curr_date} 信号触发 方向={('多' if wait_dir == 1 else '空')} "
                  f"(原趋势{trend_days}天) 等待回踩...", flush=True)

    except BacktestFinished:
        if position != 0:
            float_pnl = position * (float(klines.close.iloc[-2]) - entry_price) * LOTS * MULTIPLIER
            balance += float_pnl
            print(f"\n期末仍持仓: {entry_date} 开{('多' if position == 1 else '空')} @ {entry_price:.1f}, "
                  f"浮盈亏 {float_pnl:+.0f} 元(未计手续费)", flush=True)

        total = stats["total"]
        win = stats["win"]
        print("\n" + "=" * 50)
        print("回测结束")
        print(f"交易次数   : {total}")
        print(f"盈利/亏损  : {win} / {stats['loss']}")
        print(f"胜率       : {win / total * 100:.1f}%" if total else "胜率       : -")
        print(f"最终权益   : {balance:.2f} 元 (初始 {INIT_BALANCE})")
        print(f"收益率     : {(balance - INIT_BALANCE) / INIT_BALANCE * 100:+.2f}%")
        print("=" * 50)
    finally:
        api.close()


if __name__ == "__main__":
    main()