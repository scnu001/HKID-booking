# -*- coding: utf-8 -*-
"""
内容指纹探针 —— 真正能测出「数据什么时候变」的工具。

为什么需要它：
  5 分钟 / 52 次高频探测实测发现：
    * lastUpdateTime 在 01:00:11 / :17 / :23 之间来回跳了 24 次
    * 但九龙的有名额数**恒为 44，一次都没变**
  结论：lastUpdateTime 只是缓存生成时间戳，它变不代表数据变。
  所以必须用**内容指纹**（把全部状态拼起来算 hash）来判定真实变化。

判定：
  fingerprint 变了  =>  数据真的变了（可能有人放号/取消，也可能系统重算）
  fingerprint 没变 =>  数据没变，继续等

用法: python fingerprint_probe.py [小时数] [间隔秒]
"""
import hashlib
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

import hkid_watch as W

HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
INTERVAL = int(sys.argv[2]) if len(sys.argv) > 2 else 60
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "logs", "fingerprint.log")

os.makedirs(os.path.dirname(LOG), exist_ok=True)


def w(line):
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except OSError:
        pass


def snap():
    """返回 (lut, grid, 全部指纹, 九龙指纹, 错误)"""
    st, d = W.fetch_quota()
    if st != 200 or not d:
        return None, None, None, None, f"HTTP {st}"
    grid, _ = W.parse_quota(d)
    full = "".join(f"{k[0]}{k[1]}{v}" for k, v in sorted(grid.items()))
    ko = "".join(f"{k[1]}{v}" for k, v in sorted(grid.items()) if k[0] == "RKO")
    fp_all = hashlib.md5(full.encode()).hexdigest()[:12]
    fp_ko = hashlib.md5(ko.encode()).hexdigest()[:12]
    return d.get("lastUpdateTime"), grid, fp_all, fp_ko, None


w(f"\n{'='*72}")
w(f"内容指纹探针  启动于 {datetime.now():%Y-%m-%d %H:%M:%S}")
w(f"每 {INTERVAL}s 一次, 跑 {HOURS} 小时, 重点九龙")
w(f"{'='*72}")

t0 = time.time()
n = ok = fails = 0
last_fp = None
last_ko = None
last_lut = None
ko_open = 0
change_log = []
lut_only = 0          # 只是 lastUpdateTime 变、内容没变的次数

while time.time() - t0 < HOURS * 3600:
    n += 1
    lut, grid, fp, fpk, err = snap()
    now = datetime.now()

    if err:
        fails += 1
        w(f"[FAIL #{fails}] {err}")
        time.sleep(INTERVAL)
        continue
    fails = 0
    ok += 1

    ko = {k[1]: v for k, v in grid.items() if k[0] == "RKO"}
    ko_open = sum(1 for v in ko.values() if W.STATE_RANK.get(v, 0) >= 2)

    if last_fp is None:
        w(f"基线: 全量指纹={fp} 九龙指纹={fpk} 九龙有票 {ko_open} 天 "
          f"(官方时间 {lut})")
    elif fp != last_fp:
        # 内容真的变了
        if fpk != last_ko:
            w(f"★★★ 九龙数据真的变了! 指纹 {last_ko} -> {fpk}  "
              f"有票 {ko_open} 天  (官方 {lut})")
            # 打印九龙变化明细
            old = change_log[-1][3] if change_log else None
            for ds in sorted(ko, key=W.parse_date):
                stt = ko[ds]
                if W.STATE_RANK.get(stt, 0) >= 3:
                    pass
        else:
            w(f"*** 其它办事处变了 (九龙未变) 指纹 {last_fp} -> {fp}  "
              f"(官方 {lut})")
        change_log.append((now, lut, fp, dict(ko)))
    elif lut != last_lut:
        lut_only += 1
        # 不刷屏，只计数

    if n % 20 == 0:
        w(f"[{now:%H:%M:%S}] 探测 {n} 次 | 指纹 {fp} (九龙 {fpk}, 有票 "
          f"{ko_open} 天) | 纯时间戳抖动 {lut_only} 次 | 真实变化 "
          f"{len(change_log)} 次")

    last_fp, last_ko, last_lut = fp, fpk, lut
    time.sleep(INTERVAL)

w(f"\n{'='*72}")
w(f"结束。探测 {n} 次 成功 {ok} 失败 {fails}")
w(f"  纯 lastUpdateTime 抖动(内容没变): {lut_only} 次")
w(f"  内容真实变化                    : {len(change_log)} 次")
for i, (t, lut, fp, ko) in enumerate(change_log):
    op = sum(1 for v in ko.values() if W.STATE_RANK.get(v, 0) >= 2)
    w(f"   #{i+1} {t:%m-%d %H:%M:%S} 九龙有票 {op} 天 (官方 {lut})")
if not change_log:
    w("  (这段时间内容毫无变化 —— 说明真的没有固定放号时间，")
    w("   官方是按需/随机重算，不是每天定点放号)")
w("=" * 72)
