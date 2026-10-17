# -*- coding: utf-8 -*-
"""
探测官方配额数据的真实刷新节律 —— 连续记录 lastUpdateTime 变化。

用法: python probe_refresh.py [小时数]
默认跑 6 小时，每 20 秒探一次，只在 lastUpdateTime 变化时打印。
"""
import os
import sys
import time
from datetime import datetime

import hkid_watch as W

HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
INTERVAL = 20
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "logs", "refresh_probe.log")

os.makedirs(os.path.dirname(LOG), exist_ok=True)


def w(line):
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


w(f"\n{'='*66}")
w(f"刷新节律探针启动  {datetime.now():%Y-%m-%d %H:%M:%S}")
w(f"每 {INTERVAL}s 探一次, 持续 {HOURS} 小时")
w(f"{'='*66}")

last_lut = None
events = []
t0 = time.time()
n = 0
fails = 0

while time.time() - t0 < HOURS * 3600:
    n += 1
    st, d = W.fetch_quota()
    now = datetime.now()
    if st != 200 or not d:
        fails += 1
        w(f"{now:%H:%M:%S}  [FAIL #{fails}] HTTP {st}")
        time.sleep(INTERVAL)
        continue

    lut = d.get("lastUpdateTime")
    if lut != last_lut:
        # 解析成时间戳，方便算间隔
        try:
            t = datetime.strptime(lut, "%m/%d/%Y %H:%M:%S")
        except Exception:
            t = None
        lag = f" | 数据比本地慢 {(now - t).total_seconds():.0f}s" if t else ""
        gap = ""
        if events and t:
            prev = events[-1][1]
            if prev:
                gap = f" | 距上次放号 {(t - prev).total_seconds()/3600:.2f} 小时"
        w(f"{now:%H:%M:%S}  ★ 数据更新  lastUpdateTime={lut}{lag}{gap}")
        if t:
            events.append((now, t))
        last_lut = lut
    elif n % 15 == 0:
        w(f"{now:%H:%M:%S}  (无变化, 已探 {n} 次)")

    time.sleep(INTERVAL)

w(f"\n{'='*66}")
w(f"探针结束。总探测 {n} 次, 失败 {fails} 次, 发现 {len(events)} 次数据更新")
for i, (a, b) in enumerate(events):
    print_hk = f"{b:%H:%M:%S}"
    gap = f"  间隔 {(b - events[i-1][1]).total_seconds()/3600:.2f}h" if i else ""
    w(f"  #{i+1} 官方数据时间 {b:%Y-%m-%d %H:%M:%S} (香港时间 {print_hk}){gap}")
w("=" * 66)
