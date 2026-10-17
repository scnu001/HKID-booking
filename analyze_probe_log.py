# -*- coding: utf-8 -*-
"""
分析 12 小时探针日志，提取放号时刻分布。

用法: python analyze_probe_log.py [日志路径]
"""
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    HERE, "logs", "probe_v2.log")
if not os.path.exists(LOG):
    # 退化到工作区那份
    alt = r"C:\Users\zheng\WorkBuddy\杂事\hkid-quota-watcher\logs\probe_v2.log"
    LOG = alt if os.path.exists(alt) else LOG

if not os.path.exists(LOG):
    print("找不到探针日志:", LOG)
    sys.exit(1)

print("日志:", LOG)
print("=" * 72)

lines = open(LOG, encoding="utf-8", errors="replace").read().splitlines()

# 解析事件行
pat = re.compile(
    r"(\d{2}:\d{2}:\d{2})\s+★★★\s+名额集合变化!\s+\+(\d+)\s*/\s*-(\d+)")
events = []
cur = None
for ln in lines:
    m = pat.search(ln)
    if m:
        t = m.group(1)
        add, rem = int(m.group(2)), int(m.group(3))
        if cur and (int(t[:2]) * 60 + int(t[3:5])) - \
                (int(cur[0][:2]) * 60 + int(cur[0][3:5])) <= 2:
            cur[1] += add
            cur[2] += rem
        else:
            cur = [t, add, rem]
            events.append(cur)
    elif "基线" in ln and "启动" not in ln:
        m2 = re.search(r"(\d{2}:\d{2}:\d{2})\s+基线", ln)
        if m2:
            base = m2.group(1)
            m3 = re.search(r"(\d+)\s*个有名额", ln)
            base_n = int(m3.group(1)) if m3 else None

print(f"合并抖动后，真实变化事件: {len(events)} 次\n")

# 按小时分布
by_hour = defaultdict(lambda: {"add": 0, "rem": 0, "n": 0})
for t, a, r in events:
    h = int(t[:2])
    by_hour[h]["add"] += a
    by_hour[h]["rem"] += r
    by_hour[h]["n"] += 1

print("按小时分布（香港时间）")
print("  时段   事件数   新增   减少   净变化")
for h in sorted(by_hour):
    d = by_hour[h]
    net = d["add"] - d["rem"]
    bar = "█" * min(30, d["add"])
    print(f"  {h:02d}:00  {d['n']:>5}  {d['add']:>5}  {d['rem']:>5}  "
          f"{net:>+6}  {bar}")

# 找出新增高峰
peak = max(events, key=lambda e: e[1])
print(f"\n最大单次放号: {peak[0]}  +{peak[1]} 个")

# 有新增的事件
adds = [e for e in events if e[1] > 0]
print(f"\n有新增的事件 ({len(adds)} 次):")
for t, a, r in adds:
    print(f"  {t}  +{a:>3} / -{r:<3}")

# 长时间静默
print("\n静默时段（无变化的连续区间）")
quiet = []
for i in range(1, len(events)):
    t0 = datetime.strptime(events[i-1][0], "%H:%M:%S")
    t1 = datetime.strptime(events[i][0], "%H:%M:%S")
    gap = (t1 - t0).total_seconds()
    if gap >= 3600:
        quiet.append((events[i-1][0], events[i][0], gap / 3600))
if quiet:
    for a, b, g in quiet:
        print(f"  {a} → {b}   静默 {g:.1f} 小时")
else:
    print("  (无超过 1 小时的静默)")

if base_n:
    print(f"\n基线名额: {base_n}")
    total_add = sum(e[1] for e in events)
    total_rem = sum(e[2] for e in events)
    print(f"12 小时累计: +{total_add} / -{total_rem} = 净 {total_add-total_rem}")
print("=" * 72)
