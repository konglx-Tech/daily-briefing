#!/usr/bin/env python3
"""开盘简报共享库 —— 数据采集 / 分析 / 格式化 / 推送"""

import os
import sys
import time
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
SEND_KEY = os.environ["SEND_KEY"]
PUSH_URL = f"https://sctapi.ftqq.com/{SEND_KEY}.send"

EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

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

POLICY_KEYWORDS = {
    "high": [
        "加息", "降息", "降准", "政策", "监管", "制裁", "关税", "贸易战",
        "政治局", "国务院", "央行", "证监会", "银保监", "财政部",
        "汇率", "国债", "流动性", "货币政策", "财政政策", "社融",
        "LPR", "MLF", "逆回购", "存款准备金",
    ],
    "medium": [
        "新能源", "芯片", "半导体", "AI", "人工智能", "数据", "算力",
        "机器人", "低空经济", "自动驾驶", "量子", "生物医药",
        "基建", "房地产", "汽车", "消费", "出口", "光伏", "储能",
    ],
}

# ---------------------------------------------------------------------------
# 数据采集 —— 指数 & 美股
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


# ---------------------------------------------------------------------------
# 数据采集 —— 北向资金 & 板块资金（修复版）
# ---------------------------------------------------------------------------

def fetch_north_flow() -> dict | None:
    """北向资金 — stock_hsgt_fund_flow_summary_em，走 datacenter-web 域名，海外可达"""
    try:
        import akshare as ak
        df = ak.stock_hsgt_fund_flow_summary_em()
        if df is None or df.empty:
            return None
        north = df[df["资金方向"] == "北向"]
        if north.empty:
            return None
        today = north["交易日"].max()
        today_data = north[north["交易日"] == today]
        total = today_data["成交净买额"].sum()
        return {"net_flow": float(total)}
    except Exception as e:
        print(f"[WARN] 北向资金失败: {e}")
        return None


def fetch_sector_flow() -> list[dict]:
    """行业资金流向 Top5 — 主：同花顺（海外可达），备：东方财富"""
    try:
        import akshare as ak
        df = ak.stock_fund_flow_industry(symbol="即时")
        if df is not None and not df.empty:
            top = df.head(5)
            results = []
            for _, row in top.iterrows():
                results.append({
                    "name": str(row["行业"]),
                    "net_flow": float(row["净额"]),
                    "change_pct": float(row["行业-涨跌幅"]) if pd.notna(row["行业-涨跌幅"]) else 0,
                    "leader": str(row.get("领涨股", "")),
                })
            return results
    except Exception as e:
        print(f"[WARN] 同花顺板块资金失败: {e}")

    # fallback: 东方财富 push2（可能被海外封）
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": "5", "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2", "fid": "f62",
            "fs": "m:90 t:2",
            "fields": "f12,f14,f2,f3,f62,f184,f66",
        }
        r = requests.get(url, params=params, headers=EM_HEADERS, timeout=15)
        items = r.json().get("data", {}).get("diff", [])
        if not items:
            return []
        results = []
        for it in items[:5]:
            results.append({
                "name": it.get("f14", ""),
                "net_flow": float(it.get("f62", 0) or 0) / 1e8,
                "change_pct": float(it.get("f3", 0) or 0),
                "leader": "",
            })
        return results
    except Exception as e:
        print(f"[WARN] 东方财富板块资金fallback失败: {e}")
        return []


# ---------------------------------------------------------------------------
# 数据采集 —— 午间实时
# ---------------------------------------------------------------------------

def fetch_midday_indices() -> list[dict]:
    """午间指数快照 — stock_zh_index_spot_sina"""
    try:
        import akshare as ak
        df = ak.stock_zh_index_spot_sina()
        if df is None or df.empty:
            return []
        targets = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指"}
        results = []
        for _, row in df.iterrows():
            code = str(row["代码"])
            if code in targets:
                results.append({
                    "name": targets[code],
                    "price": float(row["最新价"]),
                    "change_pct": float(row["涨跌幅"]),
                    "open": float(row["今开"]),
                    "high": float(row["最高"]),
                    "low": float(row["最低"]),
                    "volume_wan": float(row["成交量"]) / 10000 if pd.notna(row["成交量"]) else 0,
                    "turnover_yi": float(row["成交额"]) / 1e8 if pd.notna(row["成交额"]) else 0,
                })
        return results
    except Exception as e:
        print(f"[WARN] 午间指数失败: {e}")
        return []


def fetch_midday_breadth() -> dict | None:
    """市场宽度 — stock_market_activity_legu"""
    try:
        import akshare as ak
        df = ak.stock_market_activity_legu()
        if df is None or df.empty:
            return None
        data = dict(zip(df["item"], df["value"]))
        return {
            "up": int(data.get("上涨", 0)),
            "down": int(data.get("下跌", 0)),
            "flat": int(data.get("平盘", 0)),
            "limit_up": int(data.get("涨停", 0)),
            "limit_down": int(data.get("跌停", 0)),
            "active_pct": float(data.get("活跃度", "0%").replace("%", "")) if isinstance(data.get("活跃度"), str) else 0,
        }
    except Exception as e:
        print(f"[WARN] 市场宽度失败: {e}")
        return None


def fetch_limit_pool() -> dict | None:
    """涨停/跌停股池"""
    today = datetime.now().strftime("%Y%m%d")
    try:
        import akshare as ak
        up_df = ak.stock_zt_pool_em(date=today)
        down_df = ak.stock_zt_pool_dtgc_em(date=today)
        up_count = len(up_df) if up_df is not None else 0
        down_count = len(down_df) if down_df is not None else 0

        # 连板股
        multi_board = []
        if up_df is not None and not up_df.empty and "连板数" in up_df.columns:
            multi = up_df[up_df["连板数"] >= 2]
            for _, row in multi.head(5).iterrows():
                multi_board.append(f"{row['名称']}({int(row['连板数'])}板)")

        # 涨停最多的行业
        top_industry = ""
        if up_df is not None and not up_df.empty and "所属行业" in up_df.columns:
            ind_counts = up_df["所属行业"].value_counts()
            if len(ind_counts) > 0:
                top_industry = f"{ind_counts.index[0]}({ind_counts.iloc[0]}家)"

        return {
            "limit_up_count": up_count,
            "limit_down_count": down_count,
            "multi_board": multi_board,
            "top_industry": top_industry,
        }
    except Exception as e:
        print(f"[WARN] 涨停股池失败: {e}")
        return None


# ---------------------------------------------------------------------------
# 数据采集 —— 政策 & 新闻
# ---------------------------------------------------------------------------

def fetch_policy_news() -> list[dict]:
    """合并财联社电报 + 东方财富财经早餐，关键词打分去重，取 Top 8"""
    items = []

    # 财联社电报
    try:
        import akshare as ak
        df = ak.stock_info_global_cls("重点")
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                title = str(row["标题"])
                content = str(row.get("内容", ""))
                items.append({"title": title, "summary": content[:120], "source": "财联社"})
    except Exception as e:
        print(f"[WARN] 财联社电报失败: {e}")

    # 东方财富财经早餐
    try:
        import akshare as ak
        df = ak.stock_info_cjzc_em()
        if df is not None and not df.empty:
            today_str = datetime.now().strftime("%Y-%m-%d")
            for _, row in df.head(20).iterrows():
                pub_time = str(row.get("发布时间", ""))
                if today_str in pub_time or "今日" in pub_time:
                    items.append({
                        "title": str(row["标题"]),
                        "summary": str(row.get("摘要", ""))[:120],
                        "source": "财经早餐",
                    })
    except Exception as e:
        print(f"[WARN] 财经早餐失败: {e}")

    # 打分
    scored = []
    for it in items:
        score = 0
        full = it["title"] + it["summary"]
        for kw in POLICY_KEYWORDS["high"]:
            if kw in full:
                score += 5
        for kw in POLICY_KEYWORDS["medium"]:
            if kw in full:
                score += 3
        if it["source"] == "财联社":
            score += 2
        scored.append({**it, "score": score})

    scored.sort(key=lambda x: x["score"], reverse=True)

    # 去重（相似标题只保留最高分）
    deduped = []
    seen_titles = []
    for s in scored:
        if s["score"] <= 0:
            continue
        is_dup = False
        for seen in seen_titles:
            if len(set(s["title"][:6]) & set(seen[:6])) >= 4:
                is_dup = True
                break
        if not is_dup:
            deduped.append(s)
            seen_titles.append(s["title"])
        if len(deduped) >= 8:
            break

    return deduped


# ---------------------------------------------------------------------------
# 数据采集 —— 财报 & 近一月关注
# ---------------------------------------------------------------------------

def fetch_earnings_calendar(days_ahead: int = 7) -> list[dict]:
    """近期财报日历"""
    today = datetime.now()
    try:
        import akshare as ak
        df = ak.news_report_time_baidu(date=today.strftime("%Y%m%d"))
        if df is None or df.empty:
            return []

        cutoff = today + timedelta(days=days_ahead)
        results = []
        for _, row in df.iterrows():
            pub_date_str = str(row.get("发布日期", ""))
            try:
                pub_date = datetime.strptime(pub_date_str, "%Y-%m-%d")
            except ValueError:
                continue
            if today <= pub_date <= cutoff:
                # 筛A股+港股
                exchange = str(row.get("交易所", ""))
                if exchange not in ("sh", "sz", "hk", "US", "SH", "SZ", "HK"):
                    continue
                market_val = row.get("市值", None)
                try:
                    market_val = float(market_val) if pd.notna(market_val) else 0
                except (ValueError, TypeError):
                    market_val = 0
                results.append({
                    "name": str(row["股票简称"]),
                    "code": str(row["股票代码"]),
                    "exchange": exchange,
                    "report_type": str(row.get("财报类型", "")),
                    "pub_date": pub_date_str,
                    "market_val": market_val,
                })

        results.sort(key=lambda x: x["market_val"], reverse=True)
        return results[:10]
    except Exception as e:
        print(f"[WARN] 财报日历失败: {e}")
        return []


def fetch_monthly_focus() -> list[dict]:
    """近一月重点关注 — 经济日历 + 业绩预告 + 机构评级 + 龙虎榜活跃股"""
    focuses = []
    today = datetime.now()
    cutoff = today + timedelta(days=30)

    # 1. 经济日历（降级优雅）
    try:
        import akshare as ak
        df = ak.news_economic_baidu(date=today.strftime("%Y%m%d"))
        if df is not None and not df.empty:
            for _, row in df.head(15).iterrows():
                pub_date = str(row.get("日期", ""))
                importance = str(row.get("重要性", ""))
                event = str(row.get("事件", ""))
                country = str(row.get("国家", ""))
                # 只关注中国/美国/全球
                if any(c in country for c in ["中国", "美国", "欧元区"]):
                    focuses.append({
                        "type": "经济日历",
                        "date": pub_date,
                        "title": f"{country} {event}",
                        "importance": importance,
                        "score": 8 if "****" in importance else (5 if "***" in importance else 3),
                    })
    except Exception as e:
        print(f"[WARN] 经济日历失败: {e}")

    # 2. 业绩预告
    try:
        import akshare as ak
        q = (today.month - 1) // 3 + 1
        quarter_end = f"{today.year}{q*3:02d}31"
        result = ak.stock_yjyg_em(date=quarter_end)
        if result is not None:
            df = result if hasattr(result, 'iterrows') else pd.DataFrame(result)
            if not df.empty:
                # 筛出预增/预减幅度大的
                for _, row in df.head(30).iterrows():
                    chg_low = float(row.get("净利润变动幅度下限", 0) or 0)
                    chg_high = float(row.get("净利润变动幅度上限", 0) or 0)
                    if abs(chg_low) > 100 or abs(chg_high) > 100:
                        forecast_type = str(row.get("预告类型", ""))
                        focuses.append({
                            "type": "业绩预告",
                            "date": str(row.get("报告期", "")),
                            "title": f"{row['股票简称']} {forecast_type} {chg_low:.0f}%~{chg_high:.0f}%",
                            "score": 6 if "大增" in forecast_type or "预增" in forecast_type else 4,
                        })
    except Exception as e:
        print(f"[WARN] 业绩预告失败: {e}")

    # 3. 机构推荐（最新评级）
    try:
        import akshare as ak
        df = ak.stock_institute_recommend(symbol="最新投资评级")
        if df is not None and not df.empty:
            for _, row in df.head(10).iterrows():
                stock_name = str(row.get("股票名称", ""))
                rating = str(row.get("最新评级", ""))
                rating_date = str(row.get("评级日期", ""))
                focuses.append({
                    "type": "机构推荐",
                    "date": rating_date,
                    "title": f"{stock_name} {rating}",
                    "score": 5,
                })
    except Exception as e:
        err = str(e)[:80]
        print(f"[WARN] 机构推荐失败: {err}")

    # 4. 龙虎榜活跃股
    try:
        import akshare as ak
        df = ak.stock_lhb_stock_statistic_em(symbol="近一月")
        if df is not None and not df.empty:
            for _, row in df.head(8).iterrows():
                name = str(row.get("名称", ""))
                times = row.get("上榜次数", "?")
                net_buy = row.get("龙虎榜净买额", 0)
                net_buy_yi = float(net_buy) / 1e8 if pd.notna(net_buy) and net_buy != 0 else 0
                focuses.append({
                    "type": "龙虎榜活跃",
                    "date": "近一月",
                    "title": f"{name} 上榜{times}次 净买{net_buy_yi:.1f}亿",
                    "score": 4,
                })
    except Exception as e:
        print(f"[WARN] 龙虎榜失败: {e}")

    focuses.sort(key=lambda x: x["score"], reverse=True)
    return focuses[:12]


# ---------------------------------------------------------------------------
# 分析引擎
# ---------------------------------------------------------------------------

def strategist_analysis_morning(
    us_indices: list[dict | None],
    nasdaq_vol: float | None,
    north: dict | None,
    sectors: list[dict],
    policy_news: list[dict],
    earnings: list[dict],
    monthly: list[dict],
) -> str:
    lines = []

    # 隔夜美股情绪
    valid_us = [u for u in us_indices if u is not None]
    if valid_us:
        mood_parts = []
        up = down = 0
        for u in valid_us:
            prev = u.get("open", u["close"])
            chg = (u["close"] - prev) / prev * 100 if prev else 0
            short = u["name"].replace("琼斯", "").replace("达克", "").replace("普500", "")
            mood_parts.append(f"{short}{'+' if chg>=0 else ''}{chg:.2f}%")
            if chg > 0.3:
                up += 1
            elif chg < -0.3:
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
            lines.append(f"⚠ 北向大幅净流出{abs(nf):.1f}亿，警惕")

    # 行业资金方向
    if sectors:
        top3 = [f"{s['name']}({'+' if s['net_flow']>=0 else ''}{s['net_flow']:.1f}亿)" for s in sectors[:3]]
        lines.append(f"资金聚集: {' | '.join(top3)}")

    # 政策热点摘要
    if policy_news:
        top_policies = [p for p in policy_news if p["score"] >= 3][:4]
        if top_policies:
            lines.append("政策关注:")
            for p in top_policies:
                lines.append(f"  • {p['title'][:60]}")
    else:
        lines.append("政策热点: 暂无")

    # 近一月优先级
    lines.append("")
    lines.append("【近期关注（按优先级）】")
    added = 0
    for m in monthly[:8]:
        tag = {"经济日历": "📅", "业绩预告": "📊", "机构推荐": "⭐", "龙虎榜活跃": "🔥"}.get(m["type"], "•")
        date_str = f" ({m['date']})" if m.get("date") else ""
        lines.append(f"  {tag} {m['title']}{date_str}")
        added += 1
    if added == 0:
        lines.append("  （暂无近期事件）")

    # 风险
    lines.append("")
    lines.append("【风险提示】")
    lines.append("- AI分析不构成投资建议 | 9:15集合竞价后最终判断")
    lines.append("- 关注盘前突发政策/海外事件")

    return "\n".join(lines)


def strategist_analysis_midday(
    indices: list[dict],
    breadth: dict | None,
    north: dict | None,
    sectors: list[dict],
    limit_pool: dict | None,
) -> str:
    lines = []

    # 上午走势
    if indices:
        sh = next((i for i in indices if "上证" in i["name"]), None)
        if sh:
            amplitude = (sh["high"] - sh["low"]) / sh["open"] * 100 if sh["open"] else 0
            if sh["change_pct"] > 0.5:
                lines.append(f"上午: 强势（振幅{amplitude:.1f}%）")
            elif sh["change_pct"] > 0:
                lines.append(f"上午: 偏强震荡（振幅{amplitude:.1f}%）")
            elif sh["change_pct"] > -0.5:
                lines.append(f"上午: 偏弱震荡（振幅{amplitude:.1f}%）")
            else:
                lines.append(f"上午: 弱势（振幅{amplitude:.1f}%）")
            # 量能
            if sh.get("turnover_yi", 0) > 0:
                lines.append(f"沪市半日成交: {sh['turnover_yi']:.0f}亿")

    # 赚钱效应
    if breadth:
        total = breadth["up"] + breadth["down"]
        if total > 0:
            ratio = breadth["up"] / breadth["down"] if breadth["down"] > 0 else 99
            lines.append(
                f"涨{breadth['up']} / 跌{breadth['down']} "
                f"涨停{breadth['limit_up']} / 跌停{breadth['limit_down']} "
                f"（比 {ratio:.1f}:1）"
            )

    # 连板高度
    if limit_pool and limit_pool.get("multi_board"):
        lines.append(f"连板: {' | '.join(limit_pool['multi_board'])}")
    if limit_pool and limit_pool.get("top_industry"):
        lines.append(f"涨停集中: {limit_pool['top_industry']}")

    # 午间资金（盘中北向数据通常在15:00后更新，午间值=上一交易日收盘数据）
    if north:
        nf = north["net_flow"]
        if abs(nf) < 0.01:
            lines.append(f"北向: 盘中数据待更新")
        else:
            d = "流入" if nf >= 0 else "流出"
            lines.append(f"北向: {d}{abs(nf):.1f}亿")
    if sectors:
        top3 = " | ".join([f"{s['name']}+{s['net_flow']:.1f}亿" for s in sectors[:3] if s['net_flow'] > 0])
        if top3:
            lines.append(f"板块: {top3}")

    # 下午关注
    lines.append("")
    lines.append("【下午关注】")
    lines.append("- 北向尾盘动向（14:30后）")
    lines.append("- 连板股炸板情况")
    if breadth and breadth["limit_up"] < 30:
        lines.append("- 涨停家数偏少，情绪偏弱")
    lines.append("- AI分析，不构成投资建议")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------

def _chg_str(current: float, prev: float) -> str:
    chg = (current - prev) / prev * 100 if prev else 0
    s = "+" if chg >= 0 else ""
    return f"{s}{chg:.2f}%"


def format_morning_report(
    a_shares: list[dict | None],
    us_indices: list[dict | None],
    nasdaq_vol: float | None,
    north: dict | None,
    sectors: list[dict],
    policy_news: list[dict],
    earnings: list[dict],
    monthly: list[dict],
    strategy: str,
) -> str:
    wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][datetime.now().weekday()]
    date_str = datetime.now().strftime("%Y-%m-%d")

    sections = [f"## 每日开盘简报 | {date_str} {wd}", ""]

    # 隔夜美股
    sections.append("### 隔夜美股")
    for u in us_indices:
        if u is None:
            continue
        prev = u.get("open", u["close"])
        sections.append(f"- {u['name']}: {u['close']:,.2f} ({_chg_str(u['close'], prev)})")
    if nasdaq_vol:
        sections.append(f"- 纳指年化波动: {nasdaq_vol}%")
    sections.append("")

    # A股昨日
    sections.append("### A股昨日收盘")
    for a in a_shares:
        if a is None:
            continue
        sections.append(f"- {a['name']}: {a['close']:,.2f} ({_chg_str(a['close'], a['open'])})")
    sections.append("")

    # 北向
    if north:
        sections.append("### 北向资金")
        d = "净流入" if north["net_flow"] >= 0 else "净流出"
        sections.append(f"- {d} {abs(north['net_flow']):.1f} 亿元")
        sections.append("")
    else:
        sections.append("### 北向资金")
        sections.append("- 数据暂不可用")
        sections.append("")

    # 板块资金
    if sectors:
        sections.append("### 板块资金Top5")
        for s in sectors:
            sign = "+" if s["net_flow"] >= 0 else ""
            leader = f"（领涨: {s['leader']}）" if s.get("leader") else ""
            sections.append(f"- {s['name']}: {sign}{s['net_flow']:.1f}亿 {leader}")
        sections.append("")
    else:
        sections.append("### 板块资金")
        sections.append("- 数据暂不可用")
        sections.append("")

    # 政策热点
    if policy_news:
        sections.append("### 政策/产业热点")
        for p in policy_news[:6]:
            tag = {"财联社": "⚡", "财经早餐": "📰"}.get(p["source"], "•")
            sections.append(f"- {tag} {p['title'][:80]}")
        sections.append("")

    # 财报日历
    if earnings:
        sections.append("### 近期财报日历")
        for e in earnings[:6]:
            mv = f"{e['market_val']/1e8:.0f}亿" if e.get("market_val") and e["market_val"] > 0 else ""
            sections.append(f"- {e['pub_date']} {e['name']}({e['report_type']}) {mv}")
        sections.append("")

    # 军师视角
    sections.append("### 军师视角")
    sections.append(strategy)

    return "\n".join(sections)


def format_midday_report(
    indices: list[dict],
    breadth: dict | None,
    north: dict | None,
    sectors: list[dict],
    limit_pool: dict | None,
    strategy: str,
) -> str:
    wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][datetime.now().weekday()]
    date_str = datetime.now().strftime("%Y-%m-%d")

    sections = [f"## 午间快报 | {date_str} {wd}", ""]

    # 上午收盘
    sections.append("### 上午收盘")
    for idx in indices:
        sections.append(
            f"- {idx['name']}: {idx['price']:,.2f} "
            f"（{'+' if idx['change_pct']>=0 else ''}{idx['change_pct']:.2f}%）"
            f" 成交{idx.get('turnover_yi', 0):.0f}亿"
        )
    sections.append("")

    # 赚钱效应
    if breadth:
        sections.append("### 赚钱效应")
        sections.append(f"- 上涨 {breadth['up']} / 下跌 {breadth['down']} / 平盘 {breadth['flat']}")
        sections.append(f"- 涨停 {breadth['limit_up']} / 跌停 {breadth['limit_down']}")
        sections.append("")

    # 涨停动向
    if limit_pool:
        if limit_pool.get("multi_board"):
            sections.append(f"### 连板股: {' | '.join(limit_pool['multi_board'])}")
        if limit_pool.get("top_industry"):
            sections.append(f"### 涨停集中: {limit_pool['top_industry']}")
        sections.append("")

    # 资金面
    if north:
        d = "净流入" if north["net_flow"] >= 0 else "净流出"
        sections.append(f"### 北向: {d} {abs(north['net_flow']):.1f} 亿")
    if sectors:
        top3 = " | ".join([f"{s['name']}+{s['net_flow']:.1f}亿" for s in sectors[:3] if s['net_flow'] > 0])
        if top3:
            sections.append(f"### 板块: {top3}")
    if north or sectors:
        sections.append("")

    # 军师视角
    sections.append("### 午间军师")
    sections.append(strategy)

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# 推送
# ---------------------------------------------------------------------------

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
