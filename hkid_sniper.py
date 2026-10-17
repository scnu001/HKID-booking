#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
放号时段精准守候 —— hkid_watch.py 的调度增强版
=================================================

背景（实测 + 官方资料）：
  * 官方预约窗口 = 未来 96 个工作天，**每天滚动前移 1 个工作天**
  * 官方配额数据 `lastUpdateTime` 每天只变一次（实测 00:15 前后更新）
  * 社区经验：热门办事处名额常在放号后 **1 分钟内被抢空**（开学季尤其）
  * 所以真正的策略不是全天轮询，而是：**窗口前滚那一刻密集探测，其余时间低频**

四种模式：
  1) normal  常规低频（默认 180s）—— 捡漏、观察用
  2) burst   放号冲刺   —— 在指定时刻前密集探测，秒级抓变化
  3) daily   每日定时   —— 每天在指定时刻(如 00:05)自动进入 burst，盯 30 分钟
  4) probe   只跑节律探针

示例：
  # 每天 00:05 自动开始密集盯 30 分钟
  python hkid_sniper.py daily --at 00:05 --minutes 30

  # 只在今晚 22:00 密集盯 10 分钟
  python hkid_sniper.py burst --at 22:00 --minutes 10

  # 先看看现在什么情况
  python hkid_sniper.py once
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta

import hkid_watch as W
import notify as notifier

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG = os.path.join(LOG_DIR, f"sniper-{datetime.now():%Y%m%d}.log")

# 冲刺阶段的轮询间隔（秒）。前 90 秒最密，之后逐步放缓
BURST_SCHEDULE = [
    (0, 3),      # 前 3 秒: 每 3s
    (3, 5),      # 3-15s:  每 5s
    (15, 10),    # 15-45s: 每 10s
    (45, 20),    # 45-90s: 每 20s
    (90, 30),    # 90s 之后: 每 30s
]


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {level:<5} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {level:<5} {msg}\n")
    except OSError:
        pass


def next_interval(elapsed):
    """按冲刺节奏返回下次间隔"""
    for cap, iv in BURST_SCHEDULE:
        if elapsed < cap:
            return iv
    return BURST_SCHEDULE[-1][1]


def hhmm_to_dt(s, base=None):
    """'00:05' -> 今天的 00:05（已过就顺延到明天）"""
    h, m = map(int, s.split(":"))
    now = datetime.now()
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if base is not None:
        t = t.replace(day=base.day, month=base.month, year=base.year)
    if t <= now:
        t += timedelta(days=1)
    return t


def watch_burst(minutes, only, within, min_state, open_on_hit, notify_on_hit,
                baseline=True):
    """密集守候窗口"""
    log(f"进入冲刺模式, 持续 {minutes} 分钟")
    t0 = time.time()
    prev_lut = None
    prev_lut_dt = None
    reported = set()
    errors = 0

    # 第一轮先建立基线，避免把「本来就有的名额」全当成新票刷屏。
    # baseline=False 时首轮也会全部播报（用于「我现在就想知道有哪些票」）。
    first = True
    baseline_keys = set()

    while time.time() - t0 < minutes * 60:
        st, d = W.fetch_quota()
        now = datetime.now()
        if st != 200 or not d:
            errors += 1
            log(f"查询失败 HTTP {st} (连续 {errors})", "WARN")
            time.sleep(5)
            continue
        errors = 0

        lut = d.get("lastUpdateTime")
        grid, offices = W.parse_quota(d)
        hits = W.summarize(grid, offices, only, within, min_state)

        # 官方数据是否刚更新。
        # 注意：lastUpdateTime 的秒数会在同一分钟内微跳(如 00:15:04 <-> 00:15:06)，
        # 说明官方是逐条写入的。所以要按「分钟」比较，否则每次都会误报"数据更新"。
        if lut != prev_lut:
            try:
                t = datetime.strptime(lut, "%m/%d/%Y %H:%M:%S")
                cur_min = t.replace(second=0, microsecond=0)
                prev_min = (prev_lut_dt.replace(second=0, microsecond=0)
                            if prev_lut_dt else None)
                if prev_min is None or cur_min > prev_min:
                    lag = (now - t).total_seconds()
                    log(f"★ 官方数据已更新到 {lut} (约 {lag:.0f} 秒前)", "ALERT")
                    if notify_on_hit and lag < 300:
                        notifier.notify("香港身份证配额已更新",
                                        f"官方数据时间 {lut}\n请立即查看！",
                                        sound=True)
                prev_lut_dt = t
            except Exception:
                if prev_lut is None:
                    log(f"官方数据时间: {lut}")
                prev_lut_dt = None
            prev_lut = lut

        # 是否出现新名额
        current = {(o, ds) for o, _, ds, _ in hits}
        if first:
            first = False
            reported = set(current)
            baseline_keys = set(current)
            if baseline:
                log(f"已建立基线: 当前 {len(current)} 个名额, "
                    f"之后只报「新变化」")
                if notify_on_hit and current:
                    try:
                        notifier.notify("守候已就位",
                                        f"基线 {len(current)} 个名额，"
                                        f"开始盯新变化", sound=False)
                    except Exception:
                        pass
            else:
                log(f"首轮共 {len(current)} 个名额, 全部列出:")
                for o, dt, ds, stt in hits[:30]:
                    log(f"    {dt}  {W.office_label(o):<10} "
                        f"[{W.STATE_CN.get(stt, stt)}]")
        else:
            new = current - reported
            for key in sorted(new, key=lambda x: x[1]):
                oid, datestr = key
                dte = W.parse_date(datestr)
                stt = grid.get((oid, datestr), "")
                msg = f"{W.office_label(oid)} {dte} [{W.STATE_CN.get(stt, stt)}]"
                log(f"★ 新名额! {msg}", "ALERT")
                print("\n" + "!" * 62)
                print(f"  ★ 新名额! {msg}")
                print(f"  立即预约: {W.BOOKING_URL}")
                print("!" * 62 + "\n", flush=True)
                reported.add(key)
                if notify_on_hit:
                    notifier.notify("香港身份证有名额了！", msg, sound=True)
                if open_on_hit:
                    import webbrowser
                    try:
                        webbrowser.open(W.BOOKING_URL)
                    except Exception:
                        pass

        # 心跳：每 2 分钟汇报一次总数
        if int(time.time() - t0) % 120 < 3:
            log(f"监控中… 当前 {len(hits)} 个名额符合条件, "
                f"本轮新发现 {len(reported - baseline_keys)} 个")

        elapsed = time.time() - t0
        time.sleep(next_interval(elapsed))

    log("冲刺窗口结束")
    st, d = W.fetch_quota()
    if d:
        grid, offices = W.parse_quota(d)
        hits = W.summarize(grid, offices, only, within, min_state)
        log(f"本轮结束, 当前 {len(hits)} 个名额符合条件")
        for o, dt, ds, stt in hits[:15]:
            log(f"    {dt}  {W.office_label(o):<10} [{W.STATE_CN.get(stt, stt)}]")


def mode_once(only, within, min_state):
    st, d = W.fetch_quota()
    if st != 200 or not d:
        print(f"查询失败 HTTP {st}")
        return 1
    grid, offices = W.parse_quota(d)
    print("官方数据时间:", d.get("lastUpdateTime"), "| 本地:", datetime.now().strftime("%H:%M:%S"))
    W.print_table(grid, offices, only, within)
    hits = W.summarize(grid, offices, only, within, min_state)
    print(f"\n符合条件: {len(hits)} 个")
    for o, dt, ds, stt in hits[:30]:
        print(f"  {dt}  {W.office_label(o):<10} [{W.STATE_CN.get(stt, stt)}]")
    return 0


def mode_daily(args, only, within, min_state):
    log("=" * 62)
    log("每日定时守候已启动")
    log(f"每天 {args.at} (香港时间) 开始密集盯 {args.minutes} 分钟")
    log(f"办事处: {', '.join(W.office_label(o) for o in only) if only else '全部'}")
    log(f"通知: {'开' if args.notify else '关'} | "
        f"自动开网页: {'开' if args.open_on_hit else '关'}")
    log("=" * 62)

    while True:
        target = hhmm_to_dt(args.at)
        wait = (target - datetime.now()).total_seconds()
        log(f"下次守候: {target:%Y-%m-%d %H:%M} (还有 {wait/3600:.1f} 小时)")

        # 分段睡，方便 Ctrl+C
        while wait > 0:
            time.sleep(min(30, wait))
            wait = (target - datetime.now()).total_seconds()

        log(f"★ 到达 {args.at}, 开始守候")
        try:
            notifier.notify("香港身份证放号时间到",
                            f"开始密集监控 {args.minutes} 分钟", sound=True)
        except Exception:
            pass

        watch_burst(args.minutes, only, within, min_state,
                    args.open_on_hit, args.notify)

        # 守完休息到明天
        nxt = hhmm_to_dt(args.at)
        rest = (nxt - datetime.now()).total_seconds()
        log(f"本轮结束, 休息 {rest/3600:.1f} 小时到明天")
        while rest > 0:
            time.sleep(min(60, rest))
            rest = (nxt - datetime.now()).total_seconds()


def main():
    ap = argparse.ArgumentParser(description="香港身份证放号时段精准守候")
    ap.add_argument("mode", choices=["normal", "burst", "daily", "probe", "once"],
                    help="normal=低频 / burst=单次冲刺 / daily=每日定时 / probe=节律探针 / once=看一次")
    ap.add_argument("--at", default="00:05", help="每日放号时刻 HH:MM (默认 00:05)")
    ap.add_argument("--minutes", type=float, default=30, help="守候时长(分钟)")
    ap.add_argument("--office", nargs="*", help="只盯这些办事处")
    ap.add_argument("--within", type=int, default=None, help="只看未来 N 天")
    ap.add_argument("--min-state", type=int, default=2, help="2=少量 3=充足")
    ap.add_argument("--interval", type=int, default=180, help="normal 模式间隔")
    ap.add_argument("--notify", action="store_true", default=True, help="开通知(默认开)")
    ap.add_argument("--no-notify", dest="notify", action="store_false", help="关通知")
    ap.add_argument("--open-on-hit", action="store_true", default=True, help="自动开网页")
    ap.add_argument("--no-open", dest="open_on_hit", action="store_false", help="不开网页")
    args = ap.parse_args()

    only = None
    if args.office:
        only = set()
        for o in args.office:
            k = o.strip().upper()
            if k in W.OFFICE_ALIASES:
                only.add(W.OFFICE_ALIASES[k])
            elif k in W.OFFICE_NAMES:
                only.add(k)
            else:
                print(f"错误: 无法识别办事处 {o}", file=sys.stderr)
                return 2

    log(f"日志: {LOG}")

    if args.mode == "once":
        return mode_once(only, args.within, args.min_state)
    if args.mode == "probe":
        os.system(f'python "{os.path.join(HERE, "probe_refresh.py")}" 8')
        return 0
    if args.mode == "burst":
        log(f"冲刺模式: 立刻开始, 持续 {args.minutes} 分钟")
        watch_burst(args.minutes, only, args.within, args.min_state,
                    args.open_on_hit, args.notify)
        return 0
    if args.mode == "daily":
        mode_daily(args, only, args.within, args.min_state)
        return 0
    # normal：低频常驻，等价于 hkid_watch 的持续轮询
    import threading
    log(f"常规低频模式, 每 {args.interval}s 查一次 (Ctrl+C 退出)")
    import hkid_watch
    sys.argv = [sys.argv[0], "--interval", str(args.interval)]
    if only:
        sys.argv += ["--office"] + list(only)
    if args.within:
        sys.argv += ["--within", str(args.within)]
    if args.notify:
        sys.argv += ["--notify"]
    if args.open_on_hit:
        sys.argv += ["--open-on-hit"]
    return hkid_watch.main()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已退出")
        sys.exit(0)
