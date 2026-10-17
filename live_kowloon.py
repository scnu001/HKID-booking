#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
九龙实时盯号 —— live_kowloon.py
=================================

先说清楚「实时」能做到什么程度（实测结论）：

  官方只提供轮询式接口 getSituation，**没有 WebSocket / SSE 推送**。
  所以做不到真正的实时推送，只能高频轮询 + 立刻比对。

  更关键的是，5 分钟 52 次高频探测实测发现：
    * lastUpdateTime 在 01:00:11 / :17 / :23 之间来回跳了 24 次
    * 但九龙「有名额天数」**恒为 44，一次都没变**

  也就是说：**没有固定的放号时刻**。官方更像「按需/随机重算」，
  而不是每天定点放号。所以盯着某个具体钟点是没用的。

  正确策略 = 持续轮询 + 内容指纹比对，一变就立刻通知。
  本脚本用**自适应退避**：数据一变立刻回到 10s 高频，
  没变化则逐步退到 3 分钟一次，兼顾及时性与不打扰官方。

四种盯法：
  1) 只盯九龙，且只关心「新增」—— 别的办事处变动不打扰你
  2) 滚动播报九龙各日期的余量变化
  3) 一有票立刻弹窗 + 响铃 + 自动打开预约页
  4) 常驻后台也行，装成计划任务每 N 分钟跑一次

用法：
  python live_kowloon.py                  # 盯九龙，自适应 10s~3min
  python live_kowloon.py --interval 5     # 更激进（注意可能触发限流）
  python live_kowloon.py --once           # 看一眼九龙现状就退出
  python live_kowloon.py --within 60      # 只关心未来 60 天
  python live_kowloon.py --quiet          # 只写日志，不弹窗
"""
import argparse
import hashlib
import json
import os
import sys
import time
import webbrowser
from datetime import datetime

import hkid_watch as W

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG = os.path.join(LOG_DIR, f"kowloon-{datetime.now():%Y%m%d}.log")

TARGET = "RKO"          # 九龙
TARGET_CN = "九龙办事处"

# 退避阶梯（秒）。
# 依据实测：5 分钟 52 次高频探测中，九龙有名额数**恒为 44，一次都没变**，
# 而 lastUpdateTime 在 01:00:11/:17/:23 之间来回跳了 24 次。
# 说明：
#   1) lastUpdateTime 变化是缓存抖动，不是数据变化 → 已按分钟去抖
#   2) 数据本身变动很稀疏，没必要一直高频轮询
# 所以退避比原版更保守，从 10s 起步、最多退到 3 分钟，兼顾及时性与礼貌。
BACKOFF = [10, 15, 25, 40, 60, 90, 120, 180]


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {level:<5} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {level:<5} {msg}\n")
    except OSError:
        pass


def fetch_kowloon():
    """返回 (lastUpdateTime, {dateStr: state}, 错误)"""
    st, d = W.fetch_quota()
    if st != 200 or not d:
        return None, None, f"HTTP {st}"
    grid, _ = W.parse_quota(d)
    ko = {k[1]: v for k, v in grid.items() if k[0] == TARGET}
    return d.get("lastUpdateTime"), ko, None


def fingerprint(ko):
    """九龙状态的内容指纹。

    为什么要这个：实测 lastUpdateTime 会因缓存生成而反复跳动，
    但内容可能完全没变。只认内容变化，才不会误报。
    """
    return hashlib.md5(
        "".join(f"{k}{v}" for k, v in sorted(ko.items())).encode()
    ).hexdigest()[:12]


def open_slots(ko):
    return {ds: v for ds, v in ko.items() if W.STATE_RANK.get(v, 0) >= 2}


def describe(ko, within):
    """生成九龙余量的一行摘要"""
    op = open_slots(ko)
    if within:
        cutoff = (datetime.now().date().toordinal() + within)
        op = {ds: v for ds, v in op.items()
              if (W.parse_date(ds).toordinal() <= cutoff)}
    if not op:
        return "暂无可约"
    dates = sorted(W.parse_date(ds) for ds in op)
    full = sum(1 for v in op.values() if v == "quota-g")
    part = sum(1 for v in op.values() if v == "quota-y")
    return (f"充足 {full} 天 / 少量 {part} 天"
            f"，最早 {dates[0].strftime('%m-%d')}")


def show_table(ko, within):
    op = open_slots(ko)
    rows = []
    for ds, v in sorted(op.items(), key=lambda x: W.parse_date(x[0])):
        d = W.parse_date(ds)
        if within and d.toordinal() > datetime.now().date().toordinal() + within:
            continue
        rows.append((d, v))
    print(f"\n  {TARGET_CN} — 有票日期（共 {len(rows)} 天）")
    if not rows:
        print("    (近期全满)")
        return
    # 按月分组紧凑显示
    line = []
    for d, v in rows[:60]:
        mark = "G" if v == "quota-g" else "Y"
        line.append(f"{d.strftime('%m-%d')}{mark}")
    for i in range(0, len(line), 10):
        print("    " + "  ".join(line[i:i + 10]))
    if len(line) > 60:
        print(f"    ... 另有 {len(line)-60} 天")


def do_once(within, quiet):
    lut, ko, err = fetch_kowloon()
    if err:
        print(f"查询失败: {err}")
        return 1
    print(f"官方数据时间: {lut}   本地: {datetime.now():%H:%M:%S}")
    show_table(ko, within)
    print(f"\n摘要: {describe(ko, within)}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="九龙实时盯号")
    ap.add_argument("--interval", type=float, default=10, help="起始轮询间隔秒(默认10)")
    ap.add_argument("--once", action="store_true", help="看一次就退出")
    ap.add_argument("--within", type=int, default=None, help="只关心未来 N 天")
    ap.add_argument("--quiet", action="store_true", help="不弹窗不响铃")
    ap.add_argument("--no-open", action="store_true", help="不自动开网页")
    ap.add_argument("--minutes", type=float, default=0, help="跑 N 分钟后退出(0=一直)")
    args = ap.parse_args()

    log("=" * 60)
    log(f"九龙实时盯号启动  起始间隔 {args.interval}s")
    if args.within:
        log(f"范围: 未来 {args.within} 天")
    log(f"弹窗/响铃: {'关' if args.quiet else '开'}  自动开网页: {'关' if args.no_open else '开'}")
    log(f"日志: {LOG}")
    log("=" * 60)

    if args.once:
        return do_once(args.within, args.quiet)

    t0 = time.time()
    n = 0
    errs = 0
    bi = 0                       # 退避档位
    prev_lut = None
    prev_slots = None
    prev_fp = None
    last_announce = 0.0

    while True:
        if args.minutes and (time.time() - t0) > args.minutes * 60:
            log("到达设定时长, 退出")
            break

        lut, ko, err = fetch_kowloon()
        n += 1

        if err:
            errs += 1
            log(f"查询失败 {err} (连续 {errs})", "WARN")
            time.sleep(min(30, 3 * errs))
            continue
        errs = 0

        slots = open_slots(ko)
        fp = fingerprint(ko)
        summary = describe(ko, args.within)

        # lastUpdateTime 的秒数会在同一分钟内反复跳（官方逐条写入），
        # 例如 01:00:17 ↔ 01:00:23 来回切。必须按「分钟」去抖，否则每次都误判。
        cur_min = lut[:16] if lut else None      # 'MM/DD/YYYY HH:MM'

        # ---- 情况 A：内容指纹变了（唯一可信的放号信号）----
        if prev_fp is not None and fp != prev_fp:
            bi = 0                      # 一变立刻回到最高频
            added = set(slots) - set(prev_slots or {})
            removed = set(prev_slots or {}) - set(slots)
            if added:
                for ds in sorted(added, key=W.parse_date):
                    v = slots[ds]
                    log(f"★★ 九龙放号! {W.parse_date(ds)} "
                        f"[{W.STATE_CN.get(v, v)}]", "ALERT")
                print(f"\n{'!'*60}")
                print(f"  九龙新增 {len(added)} 个可约日期！")
                for ds in sorted(added, key=W.parse_date)[:10]:
                    print(f"    {W.parse_date(ds)}  [{W.STATE_CN.get(slots[ds],'')}]")
                print(f"  立即预约: {W.BOOKING_URL}")
                print(f"{'!'*60}\n", flush=True)
                if not args.quiet:
                    try:
                        import notify
                        notify.notify("九龙身份证有名额！",
                                      f"新增 {len(added)} 天\n最早 "
                                      f"{W.parse_date(sorted(added, key=W.parse_date)[0])}",
                                      sound=True)
                    except Exception as e:
                        log(f"通知失败: {e}", "WARN")
                if not args.no_open:
                    try:
                        webbrowser.open(W.BOOKING_URL)
                    except Exception:
                        pass
            else:
                log(f"(内容有变但无新增: -{len(removed)} 天) "
                    f"指纹 {prev_fp} -> {fp}  {summary}")
        # ---- 情况 B：只是时间戳变了（分钟粒度），内容没变 ----
        elif cur_min != prev_lut:
            bi = min(bi + 1, 2)        # 数据在重算，但内容没变，轻度提高关注
            log(f"[时间戳更新] {lut} | 九龙: {summary}  (内容未变)")
            prev_lut = cur_min
        # ---- 情况 C：完全没变化，退避 ----
        else:
            bi = min(bi + 1, len(BACKOFF) - 1)
            if time.time() - last_announce > 600:
                log(f"[{TARGET_CN}] {summary}  |  指纹 {fp}  "
                    f"(探测 {n} 次, 下次 {BACKOFF[bi]}s 后)")
                last_announce = time.time()

        prev_slots = slots
        prev_fp = fp
        prev_lut = cur_min
        time.sleep(args.interval if bi == 0 else BACKOFF[bi])

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已退出")
        sys.exit(0)
