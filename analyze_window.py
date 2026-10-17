# -*- coding: utf-8 -*-
"""分析配额数据的滚动窗口规律，验证「每天几点放号」。"""
import hkid_watch as W
from datetime import datetime, timedelta, date

st, d = W.fetch_quota()
g, o = W.parse_quota(d)

print("=" * 66)
print("官方 lastUpdateTime :", d.get("lastUpdateTime"))
print("本地抓取时间       :", datetime.now().strftime("%m/%d/%Y %H:%M:%S"))
print("=" * 66)

# ---- 1. 各办事处最早可约日期 ----
print("\n【1】各办事处最早可约日期")
rows = []
for oid in sorted({k[0] for k in g}):
    ds = [W.parse_date(k[1]) for k, v in g.items()
          if k[0] == oid and W.STATE_RANK.get(v, 0) >= 2]
    if ds:
        rows.append((oid, min(ds), len(ds)))
for oid, mn, n in sorted(rows, key=lambda r: r[1]):
    print(f"  {W.office_label(oid):<10} 最早 {mn}   可选日期 {n} 个")

# ---- 2. 96 工作天窗口的起点 ----
alld = sorted({W.parse_date(k[1]) for k in g})
print(f"\n【2】窗口范围: {alld[0]}  →  {alld[-1]}  共 {len(alld)} 个日期")

# ---- 3. 关键：最早有票的日期 vs 今天的距离（工作天）----
today = date.today()
def workdays_between(a, b):
    """算 a 到 b 之间的工作天数(排除周日)"""
    n, cur = 0, a
    while cur < b:
        cur += timedelta(days=1)
        if cur.weekday() != 6:  # 6=周日
            n += 1
    return n

print(f"\n【3】今天({today})往前数, 各办事处最早有票日期是第几个工作天")
print("     (若总是稳定在 96 附近 => 每天滚动放新号)")
for oid, mn, n in sorted(rows, key=lambda r: r[1]):
    print(f"  {W.office_label(oid):<10} {mn}  = 第 {workdays_between(today, mn)} 个工作天")

# ---- 4. 按日期统计全区总量分布 ----
print("\n【4】名额的日期分布(前 25 个有票日期)")
from collections import defaultdict
by_date = defaultdict(list)
for (oid, ds), v in g.items():
    if W.STATE_RANK.get(v, 0) >= 2:
        by_date[W.parse_date(ds)].append(oid)
for dt in sorted(by_date)[:25]:
    offices = by_date[dt]
    mark = "  <<<" if workdays_between(today, dt) <= 30 else ""
    print(f"  {dt} (第{workdays_between(today,dt):2d}工作日) "
          f"{len(offices)}个办事处: {' '.join(W.office_label(x) for x in offices)}{mark}")

# ---- 5. 近期(30工作日内)到底还剩什么 ----
print("\n【5】未来 30 个工作日内的名额(这才是你现在能抢的)")
near = {k: v for k, v in g.items()
        if W.STATE_RANK.get(v, 0) >= 2
        and W.parse_date(k[1])
        and workdays_between(today, W.parse_date(k[1])) <= 30}
if near:
    for (oid, ds), v in sorted(near.items(), key=lambda x: x[0][1]):
        print(f"  {W.parse_date(ds)}  {W.office_label(oid):<10} [{W.STATE_CN.get(v,v)}]")
else:
    print("  (空) 近期全满 —— 这就是必须盯放号时刻的原因")
