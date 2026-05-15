#!/usr/bin/env python3
"""每日开盘简报 & 午间快报 —— 统一入口"""
import sys
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from briefing_lib import (
    fetch_a_share_index, fetch_us_index, fetch_nasdaq_volatility,
    fetch_north_flow, fetch_sector_flow,
    fetch_policy_news, fetch_earnings_calendar,
    fetch_monthly_focus,
    fetch_midday_indices, fetch_midday_breadth, fetch_limit_pool,
    strategist_analysis_morning, strategist_analysis_midday,
    format_morning_report, format_midday_report, push,
)

SH_INDICES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
]

US_INDICES = [
    (".DJI", "道指"),
    (".IXIC", "纳指"),
    (".INX", "标普500"),
]


def main_morning():
    print(f"=== 开盘简报 Morning {datetime.now().isoformat()} ===")

    # Step 1: 指数并行
    print("[1/5] A股+美股...")
    a_shares = []
    us = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        fut_a = {ex.submit(fetch_a_share_index, s, n): n for s, n in SH_INDICES}
        fut_u = {ex.submit(fetch_us_index, s, n): n for s, n in US_INDICES}
        fut_nv = ex.submit(fetch_nasdaq_volatility)
        for f in fut_a:
            a_shares.append(f.result())
        for f in fut_u:
            us.append(f.result())
        nasdaq_vol = fut_nv.result()

    # Step 2: 资金数据并行
    print("[2/5] 北向+板块...")
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_north = ex.submit(fetch_north_flow)
        fut_sector = ex.submit(fetch_sector_flow)
        north = fut_north.result()
        sectors = fut_sector.result()

    # Step 3: 新闻+财报并行
    print("[3/5] 政策热点+财报日历...")
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_news = ex.submit(fetch_policy_news)
        fut_earn = ex.submit(fetch_earnings_calendar, 7)
        policy_news = fut_news.result()
        earnings = fut_earn.result()

    # Step 4: 近期关注
    print("[4/5] 近一月关注...")
    monthly = fetch_monthly_focus()

    # Step 5: 分析+推送
    print("[5/5] 军师分析+推送...")
    strategy = strategist_analysis_morning(us, nasdaq_vol, north, sectors, policy_news, earnings, monthly)
    report = format_morning_report(a_shares, us, nasdaq_vol, north, sectors, policy_news, earnings, monthly, strategy)

    title = f"开盘简报 {datetime.now().strftime('%m/%d')}"
    success = push(title, report)

    try:
        print(report)
    except Exception:
        print("[INFO] 简报已推送微信")
    sys.exit(0 if success else 1)


def main_midday():
    print(f"=== 午间快报 Midday {datetime.now().isoformat()} ===")

    # Step 1: 指数+宽度并行
    print("[1/4] 上午指数+市场宽度...")
    with ThreadPoolExecutor(max_workers=3) as ex:
        fut_idx = ex.submit(fetch_midday_indices)
        fut_brd = ex.submit(fetch_midday_breadth)
        fut_lim = ex.submit(fetch_limit_pool)
        indices = fut_idx.result()
        breadth = fut_brd.result()
        limit_pool = fut_lim.result()

    # Step 2: 资金面
    print("[2/4] 北向+板块...")
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_north = ex.submit(fetch_north_flow)
        fut_sector = ex.submit(fetch_sector_flow)
        north = fut_north.result()
        sectors = fut_sector.result()

    # Step 3: 分析
    print("[3/4] 军师分析...")
    strategy = strategist_analysis_midday(indices, breadth, north, sectors, limit_pool)

    # Step 4: 格式化+推送
    print("[4/4] 格式化+推送...")
    report = format_midday_report(indices, breadth, north, sectors, limit_pool, strategy)

    title = f"午间快报 {datetime.now().strftime('%m/%d')}"
    success = push(title, report)

    try:
        print(report)
    except Exception:
        print("[INFO] 快报已推送微信")
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["morning", "midday"], required=True)
    args = parser.parse_args()

    if args.mode == "morning":
        main_morning()
    else:
        main_midday()
