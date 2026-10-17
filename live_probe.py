# -*- coding: utf-8 -*-
"""
高频探针 —— 用来回答两个问题：
  1) 配额数据到底是「15 分钟不变」还是「随时会变」？
  2) 九龙(RKO)的名额什么时候会从已满变成有名额？

方法：每 5 秒拉一次，只记录 lastUpdateTime 变化 + 九龙状态变化。
跑 N 分钟后输出统计。

用法: python live_probe.py [分钟数] [间隔秒]
"""
import sys
import time
from collections import Counter
from datetime import datetime

import hkid_watch as W

MINUTES = float(sys.argv[1]) if len(sys.argv) > 1 else 5
INTERVAL = float(sys.argv[2]) if len(sys.argv) > 2 else 5
FOCUS = "RKO"  # 九龙

lut_seq = []          # lastUpdateTime 变化序列
rko_seq = []          # 九龙状态指纹变化序列
t0 = time.time()
n = 0
http_fail = 0

print(f"=" * 74)
print(f"高频探针  {datetime.now():%H:%M:%S} 起，每 {INTERVAL}s 一次，跑 {MINUTES} 分钟")
print(f"重点盯：九龙 (RKO)")
print("=" * 74)

prev_lut = None
prev_rko = None

while time.time() - t0 < MINUTES * 60:
    n += 1
    st, d = W.fetch_quota()
    now = datetime.now()
    if st != 200 or not d:
        http_fail += 1
        print(f"{now:%H:%M:%S}  HTTP {st}")
        time.sleep(INTERVAL)
        continue

    lut = d.get("lastUpdateTime")
    grid, offices = W.parse_quota(d)

    # 九龙指纹：只含有票日期的状态
    rko = frozenset((k[1], v) for k, v in grid.items() if k[0] == FOCUS)
    rko_open = sorted(ds for ds, v in rko if W.STATE_RANK.get(v, 0) >= 2)

    if lut != prev_lut:
        lut_seq.append((now, lut))
        print(f"{now:%H:%M:%S}  LUT {lut}  | 九龙有票 {len(rko_open)} 个"
              f"{'  ←九龙变了!' if prev_rko is not None and rko != prev_rko else ''}")
        prev_lut = lut
    if prev_rko is not None and rko != prev_rko:
        added = set(rko_open) - set(sorted(ds for ds, v in prev_rko
                                            if W.STATE_RANK.get(v, 0) >= 2))
        removed = set(sorted(ds for ds, v in prev_rko
                             if W.STATE_RANK.get(v, 0) >= 2)) - set(rko_open)
        print(f"{now:%H:%M:%S}  *** 九龙状态变化! +{sorted(added)} -{sorted(removed)}")
        if added:
            for ds in sorted(added):
                print(f"         新增名额: {W.parse_date(ds)}  "
                      f"[{W.STATE_CN.get(dict(rko).get(ds,''),'?')}]")
    prev_rko = rko
    time.sleep(INTERVAL)

print()
print("=" * 74)
print(f"探测 {n} 次 (失败 {http_fail})，历时 {(time.time()-t0)/60:.1f} 分钟")
print(f"lastUpdateTime 变化 {len(lut_seq)} 次:")
for i, (t, l) in enumerate(lut_seq):
    gap = ""
    if i:
        g = (l - lut_seq[i-1][1]).total_seconds() if False else None
    # 解析成时间算间隔
    try:
        pt = datetime.strptime(lut_seq[i-1][1], "%m/%d/%Y %H:%M:%S")
        ct = datetime.strptime(l, "%m/%d/%Y %H:%M:%S")
        gap = f"  间隔 {(ct-pt).total_seconds():.0f}s"
    except Exception:
        pass
    print(f"  {t:%H:%M:%S}  {l}{gap}")
if len(lut_seq) >= 2:
    gaps = []
    for i in range(1, len(lut_seq)):
        try:
            a = datetime.strptime(lut_seq[i-1][1], "%m/%d/%Y %H:%M:%S")
            b = datetime.strptime(lut_seq[i][1], "%m/%d/%Y %H:%M:%S")
            gaps.append((b - a).total_seconds())
        except Exception:
            pass
    if gaps:
        print(f"  → 间隔: min={min(gaps):.0f}s  max={max(gaps):.0f}s  "
              f"平均={sum(gaps)/len(gaps):.0f}s")
print("=" * 74)
