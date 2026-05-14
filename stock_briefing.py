#!/usr/bin/env python3
"""每日开盘简报 --- A股盘前 + 隔夜美股 + 军师分析 -> 微信推送"""
import os
import sys
import json
from datetime import datetime, timedelta

import requests

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SEND_KEY = os.environ.get("SEND_KEY", "SCT349627TxQ4cKa87XP5q1cNRKseeBvcQ")
PUSH_URL = f"https://sctapi.ftqq.com/{SEND_KEY}.send"

EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


# ---------------------------------------------------------------------------
# 数据采集
# ---------------------------------------------------------------------------

def fetch_a_share_index(symbol: str, name: str) -> dict | None:
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol=symbol)
        if df is None or df.empty:
            return None
        latest = df.iloc[-1]
        return {
            "name": name,
            "close": float(latest["close"]),
            "open": float(latest["open"]),
            "high": float(latest["high"]),
            "low": float(latest["low"]),
        }
    except Exception as e:
        print(f"[WARN] A股{name}失败: {e}")
        return None


def fetch_us_index(symbol: str, name: str) -> dict | None:
    try:
        import akshare as ak
        df = ak.index_us_stock_sina(symbol=symbol)
        if df is None or df.empty:
            return None
        latest = df.iloc[-1]
        return {
            "name": name,
            "close": float(latest["close"]),
            "open": float(latest["open"]),
            "high": float(latest["high"]),
            "low": float(latest["low"]),
        }
    except Exception as e:
        print(f"[WARN] 美股{name}失败: {e}")
        return None


def fetch_nasdaq_volatility() -> float | None:
    """纳指近5日日收益率年化波动率"""
    try:
        import akshare as ak
        df = ak.index_us_stock_sina(symbol=".IXIC")
        if df is None or df.empty or len(df) < 5:
            return None
        rets = df["close"].tail(5).pct_change().dropna()
        return round(rets.std() * 100 * (252 ** 0.5), 1)
    except Exception as e:
        print(f"[WARN] 波动率失败: {e}")
        return None


def fetch_north_flow() -> dict | None:
    """北向资金 --- 直接调东方财富API"""
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": "50", "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2",
            "fid": "f62",
            "fs": "m:0 t:6",  # 北向资金
            "fields": "f12,f14,f2,f3,f62,f184,f66",
        }
        r = requests.get(url, params=params, headers=EM_HEADERS, timeout=15)
        data = r.json()
        items = data.get("data", {}).get("diff", [])
        if not items:
            return None
        total = sum(float(it.get("f62", 0) or 0) for it in items)
        return {"net_flow": total / 1e8}  # 元→亿
    except Exception as e:
        print(f"[WARN] 北向资金失败: {e}")
        return None


def fetch_sector_flow() -> list[dict]:
    """行业资金流向Top5 --- 直接调东方财富API"""
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": "10", "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2",
            "fid": "f62",
            "fs": "m:90 t:2",  # 行业资金流向
            "fields": "f12,f14,f2,f3,f62,f184,f66",
        }
        r = requests.get(url, params=params, headers=EM_HEADERS, timeout=15)
        data = r.json()
        items = data.get("data", {}).get("diff", [])
        results = []
        for it in items[:5]:
            name = it.get("f14", "")
            net = float(it.get("f62", 0) or 0) / 1e8
            results.append({"name": name, "net_flow": net})
        return results
    except Exception as e:
        print(f"[WARN] 板块资金失败: {e}")
        return []


# ---------------------------------------------------------------------------
# 军师分析
# ---------------------------------------------------------------------------

def strategist_analysis(
    a_shares: list[dict | None],
    us_indices: list[dict | None],
    nasdaq_vol: float | None,
    north: dict | None,
    sectors: list[dict],
) -> str:
    lines = []

    # 隔夜美股
    valid_us = [u for u in us_indices if u is not None]
    if valid_us:
        mood_parts = []
        up = down = 0
        for u in valid_us:
            prev = u.get("open", u["close"])
            chg = (u["close"] - prev) / prev * 100 if prev else 0
            short = u["name"].replace("琼斯","").replace("达克","").replace("普500","")
            mood_parts.append(f"{short}{'+' if chg>=0 else ''}{chg:.2f}%")
            if chg > 0:
                up += 1
            else:
                down += 1
        mood = "偏多" if up > down else ("偏空" if down > up else "震荡")
        lines.append(f"隔夜美股: {mood}（{' | '.join(mood_parts)}）")
    else:
        lines.append("隔夜美股: 数据缺失")

    if nasdaq_vol:
        if nasdaq_vol > 30:
            lines.append(f"纳指波动{nasdaq_vol}%（恐慌区间），注意避险传导")
        elif nasdaq_vol > 20:
            lines.append(f"纳指波动{nasdaq_vol}%（偏高），控制仓位")
        else:
            lines.append(f"纳指波动{nasdaq_vol}%，平稳")

    # 北向
    if north:
        nf = north["net_flow"]
        if nf > 50:
            lines.append(f"北向大幅净流入{nf:.1f}亿，外资积极")
        elif nf > 0:
            lines.append(f"北向净流入{nf:.1f}亿")
        elif nf > -50:
            lines.append(f"北向净流出{abs(nf):.1f}亿")
        else:
            lines.append(f"北向大幅净流出{abs(nf):.1f}亿，警惕")

    # 板块
    if sectors:
        top3 = [f"{s['name']}(+{s['net_flow']:.1f}亿)" for s in sectors[:3]]
        lines.append(f"资金聚集: {' | '.join(top3)}")

    # 关注方向
    lines.append("")
    lines.append("【今日关注方向】")
    if sectors:
        lines.append(f"短线: {'、'.join(s['name'] for s in sectors[:3])}")
    if north and north["net_flow"] > 0:
        lines.append("外资回流→关注北向重仓和核心资产")
    if nasdaq_vol and nasdaq_vol > 25:
        lines.append("高波动→多看少动、降仓位")

    # 风险
    lines.append("")
    lines.append("【风险提示】")
    lines.append("- AI分析，不构成投资建议")
    lines.append("- 9:15集合竞价后做最终判断")
    lines.append("- 关注盘前突发政策/海外事件")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 格式化 & 推送
# ---------------------------------------------------------------------------

def format_report(
    a_shares: list[dict | None],
    us_indices: list[dict | None],
    nasdaq_vol: float | None,
    north: dict | None,
    sectors: list[dict],
    strategy: str,
) -> str:
    wd = ["周一","周二","周三","周四","周五","周六","周日"][datetime.now().weekday()]
    date_str = datetime.now().strftime("%Y-%m-%d")

    lines = [f"## 每日开盘简报 | {date_str} {wd}", ""]

    lines.append("### 隔夜美股")
    for u in us_indices:
        if u is None:
            continue
        prev = u.get("open", u["close"])
        chg = (u["close"] - prev) / prev * 100 if prev else 0
        s = "+" if chg >= 0 else ""
        lines.append(f"- {u['name']}: {u['close']:,.2f} ({s}{chg:.2f}%)")
    if nasdaq_vol:
        lines.append(f"- 纳指年化波动: {nasdaq_vol}%")
    lines.append("")

    lines.append("### A股昨日收盘")
    for a in a_shares:
        if a is None:
            continue
        prev = a["open"]
        chg = (a["close"] - prev) / prev * 100 if prev else 0
        s = "+" if chg >= 0 else ""
        lines.append(f"- {a['name']}: {a['close']:,.2f} ({s}{chg:.2f}%)")
    lines.append("")

    if north:
        lines.append("### 北向资金")
        d = "净流入" if north["net_flow"] >= 0 else "净流出"
        lines.append(f"- {d} {abs(north['net_flow']):.1f} 亿元")
        lines.append("")

    if sectors:
        lines.append("### 板块资金Top5")
        for s in sectors:
            sign = "+" if s["net_flow"] >= 0 else ""
            lines.append(f"- {s['name']}: {sign}{s['net_flow']:.1f}亿")
        lines.append("")

    lines.append("### 军师视角")
    lines.append(strategy)

    return "\n".join(lines)


def push(title: str, content: str) -> bool:
    try:
        r = requests.post(PUSH_URL, data={"title": title, "desp": content}, timeout=15)
        result = r.json()
        if result.get("code") == 0:
            print("推送成功")
            return True
        else:
            print(f"推送失败: {result}")
            return False
    except Exception as e:
        print(f"推送异常: {e}")
        return False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    print(f"=== 开盘简报 {datetime.now().isoformat()} ===")

    print("[1/4] A股+美股...")
    a_shares = [
        fetch_a_share_index("sh000001", "上证指数"),
        fetch_a_share_index("sz399001", "深证成指"),
        fetch_a_share_index("sz399006", "创业板指"),
    ]
    us = [
        fetch_us_index(".DJI", "道指"),
        fetch_us_index(".IXIC", "纳指"),
        fetch_us_index(".INX", "标普500"),
    ]
    nasdaq_vol = fetch_nasdaq_volatility()

    print("[2/4] 北向资金...")
    north = fetch_north_flow()

    print("[3/4] 板块资金...")
    sectors = fetch_sector_flow()

    print("[4/4] 军师分析+推送...")
    strategy = strategist_analysis(a_shares, us, nasdaq_vol, north, sectors)
    report = format_report(a_shares, us, nasdaq_vol, north, sectors, strategy)

    title = f"开盘简报 {datetime.now().strftime('%m/%d')}"
    success = push(title, report)

    try:
        print(report)
    except Exception:
        print("[INFO] 简报已推送微信")

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
