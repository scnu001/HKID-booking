# -*- coding: utf-8 -*-
"""
智能刷新探针 v2 —— 区分「官方数据真的变了」和「只是秒数抖动」。

v1 的教训：lastUpdateTime 的秒数会在同一分钟内乱跳（00:15:04 ↔ 00:15:16），
因为官方是逐条写入的。用秒数比较会疯狂误报「数据更新」。

v2 只在**名额集合真的发生变化**时才算一次事件，这才是有意义的放号时刻。

用法: python probe_refresh2.py [小时数] [间隔秒]
"""
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

import hkid_watch as W

HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
INTERVAL = int(sys.argv[2]) if len(sys.argv) > 2 else 30
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "logs", "probe_v2.log")

os.makedirs(os.path.dirname(LOG), exist_ok=True)


def w(line):
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def snapshot():
    """返回 (lut, {(office,date): state}, 失败原因)"""
    st, d = W.fetch_quota()
    if st != 200 or not d:
        return None, None, f"HTTP {st}"
    grid, _ = W.parse_quota(d)
    # 只关心有票的格子，忽略秒数抖动
    hits = frozenset((k[0], k[1]) for k, v in grid.items()
                     if W.STATE_RANK.get(v, 0) >= 2)
    return d.get("lastUpdateTime"), (grid, hits), None


w(f"\n{'='*70}")
w(f"智能刷新探针 v2  启动于 {datetime.now():%Y-%m-%d %H:%M:%S}")
w(f"每 {INTERVAL}s 探一次, 跑 {HOURS} 小时")
w(f"只在「名额集合真的变了」时记录事件")
w(f"{'='*70}")

prev_lut = None
prev_hits = None
prev_grid = None
events = []
t0 = time.time()
n = ok = fails = 0

while time.time() - t0 < HOURS * 3600:
    n += 1
    lut, snap, err = snapshot()
    now = datetime.now()

    if err:
        fails += 1
        w(f"{now:%H:%M:%S}  [FAIL #{fails}] {err}")
        time.sleep(INTERVAL)
        continue
    fails = 0
    ok += 1
    grid, hits = snap

    if prev_hits is None:
        w(f"{now:%H:%M:%S}  基线建立: {len(hits)} 个有名额格子, "
          f"官方数据时间 {lut}")
    elif hits != prev_hits:
        # 真的变了 —— 这才是有意义的放号/取消
        added = hits - prev_hits
        removed = prev_hits - hits
        kinds = defaultdict(list)
        for oid, ds in added:
            kinds[W.STATE_CN.get(grid.get((oid, ds), ""), "?")].append(
                (W.office_label(oid), W.parse_date(ds)))
        detail = []
        for k, v in kinds.items():
            detail.append(f"新增[{k}]×{len(v)}")
            for nm, dd in sorted(v, key=lambda x: x[1])[:6]:
                detail.append(f"    {dd} {nm}")
        w(f"{now:%H:%M:%S}  ★★★ 名额集合变化! "
          f"+{len(added)} / -{len(removed)}")
        for d_ in detail[:30]:
            w("        " + d_)
        w(f"        官方数据时间 {lut}")
        events.append((now, lut, len(added), len(removed)))
        try:
            import notify
            notify.notify("香港身份证名额变化！",
                          f"新增 {len(added)} 个 / 减少 {len(removed)} 个\n"
                          f"请立即查看！", sound=True)
        except Exception:
            pass
    elif n % 20 == 0:
        w(f"{now:%H:%M:%S}  (无变化, 累计探测 {n} 次, {len(hits)} 个名额)")

    prev_lut, prev_hits, prev_grid = lut, hits, grid
    time.sleep(INTERVAL)

w(f"\n{'='*70}")
w(f"结束。探测 {n} 次, 成功 {ok}, 失败 {fails}, 名额变化事件 {len(events)} 次")
for i, (t, lut, a, r) in enumerate(events):
    w(f"  #{i+1} {t:%m-%d %H:%M:%S}  +{a}/-{r}  (官方 {lut})")
if not events:
    w("  (本次运行期间名额没有变化 —— 说明这段时间没放新号，")
    w("   或放号时段不在这段时间内。)")
w("=" * 70)
