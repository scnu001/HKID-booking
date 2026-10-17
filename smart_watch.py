#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
时段感知监控 —— 12 小时实测数据的直接产物。

实测结论（2108 次探测 / 12 小时 / 200 个新增名额）：

    00:00–08:00   新增 0 个    ← 完全静默
    08:00–09:00   新增 0 个
    09:00–12:00   新增 200 个  ← 100% 都在这里
        其中 09:31 一次 +71、11:32 一次 +48（两个高峰，间隔约 2 小时）
    12:00–13:00   新增 0 个（只有取消）

所以「每天 00:10 守着抢号」是错的 —— 那个时段根本不放号。

本模块按实测节律自动调整轮询频率：
  重点窗口 08:45–12:30  →  10 秒
  次要窗口 12:30–23:00  →  3–5 分钟（此时基本只有取消和新释放）
  夜间     23:00–08:45  →  15 分钟（几乎不变，纯耗资源）

用法:
  python smart_watch.py                     # 自适应盯（推荐）
  python smart_watch.py --office RKO        # 只盯九龙
  python smart_watch.py --always-fast       # 全天候 10 秒（不推荐，太吵）
  python smart_watch.py --show-plan         # 只打印今天的时段计划
"""
import argparse
import os
import sys
import time
import webbrowser
from datetime import datetime, timedelta

import hkid_watch as W
import notify as notifier

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG = os.path.join(LOG_DIR, f"smart-{datetime.now():%Y%m%d}.log")


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {level:<5} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {level:<5} {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- 时段策略
# 依据 12 小时实测（200/200 新增落在 09:00-11:59）
def current_phase(now=None):
    """返回 (阶段名, 轮询间隔秒, 说明)"""
    now = now or datetime.now()
    m = now.hour * 60 + now.minute
    t = lambda h, mi: h * 60 + mi

    if t(8, 45) <= m <= t(12, 30):
        return "重点窗口", 10, "实测放号集中区(09:00-12:00 占 100% 新增)"
    if t(12, 30) <= m < t(17, 0):
        return "次要时段", 180, "放号基本结束, 偶有取消释放"
    if t(17, 0) <= m < t(23, 0):
        return "低频时段", 300, "极少变化"
    return "夜间静默", 900, "实测 00:00-08:00 零新增, 15 分钟查一次足够"


def show_plan():
    now = datetime.now()
    print("=" * 66)
    print("今日监控时段计划（依据 12 小时 / 2108 次实测）")
    print("=" * 66)
    phases = [
        (0, 0, "夜间静默", 900),
        (8, 45, "重点窗口", 10),
        (12, 30, "次要时段", 180),
        (17, 0, "低频时段", 300),
        (23, 0, "夜间静默", 900),
    ]
    for h, m, name, iv in phases:
        end = phases[(phases.index((h, m, name, iv)) + 1) % len(phases)]
        cur = now.hour * 60 + now.minute
        start = h * 60 + m
        inside = (start <= cur < end[0] * 60 + end[1]) if start < end[0] * 60 + end[1] \
            else (cur >= start or cur < end[0] * 60 + end[1])
        mark = " <<< 当前" if inside else ""
        print(f"  {h:02d}:{m:02d}  {name:<8} 每 {iv:>3} 秒{mark}")
    print()
    ph, iv, why = current_phase()
    print(f"当前处于: {ph} (每 {iv} 秒) — {why}")
    print("=" * 66)


# ---------------------------------------------------------------- 监控

def run(offices, within, min_state, always_fast, no_open, quiet, minutes,
        banner=True):
    t0 = time.time()
    prev_fp = None
    prev_slots = None
    n = 0
    last_phase = None
    seen = set()
    errors = 0

    if banner:
        log("=" * 62)
        log("时段感知监控启动")
        log(f"办事处: {', '.join(W.office_label(o) for o in offices) if offices else '全部'}")
        log(f"日志: {LOG}")
        log("=" * 62)

    while True:
        if minutes and (time.time() - t0) > minutes * 60:
            log("到达设定时长, 退出")
            break

        st, d = W.fetch_quota()
        n += 1
        now = datetime.now()

        if st != 200 or not d:
            errors += 1
            log(f"查询失败 HTTP {st} (连续 {errors})", "WARN")
            time.sleep(min(60, 10 * errors))
            continue
        errors = 0

        grid, offices_map = W.parse_quota(d)
        scope = offices or list(offices_map.keys())
        # 关注范围内的状态：{officeId: {dateStr: state}}
        scope_q = {oid: {k[1]: v for k, v in grid.items() if k[0] == oid}
                   for oid in scope}

        import hashlib
        # 注意: sorted(dict) 返回的是 key 列表, 必须先 .items() 再 sorted
        fp = hashlib.md5(
            "".join(f"{o}{d_}{v}" for o in sorted(scope_q)
                    for d_, v in sorted(scope_q[o].items())).encode()
        ).hexdigest()[:12]

        slots = {}
        for oid, dd in scope_q.items():
            for d_, v in dd.items():
                if W.STATE_RANK.get(v, 0) >= min_state:
                    slots[(oid, d_)] = v

        # 阶段切换提示
        ph, iv, why = (("手动高频", 10, "always-fast")
                       if always_fast else current_phase(now))
        if ph != last_phase:
            log(f"进入【{ph}】阶段 — 每 {iv} 秒查一次（{why}）")
            last_phase = ph

        # 首次只建基线
        if prev_fp is None:
            prev_fp, prev_slots = fp, dict(slots)
            log(f"基线建立 指纹={fp} 关注范围内名额 {len(slots)} 个 "
                f"| 官方 {d.get('lastUpdateTime')}")
            time.sleep(iv)
            continue

        if fp == prev_fp:
            time.sleep(iv)
            continue

        # ---- 指纹变了：找新增 ----
        new = set(slots) - set(prev_slots)
        if not new:
            log(f"(内容变化但无新增) 指纹 {prev_fp}->{fp}")
            prev_fp, prev_slots = fp, dict(slots)
            time.sleep(iv)
            continue

        log(f"★★ 新增 {len(new)} 个名额! 指纹 {prev_fp}->{fp}", "ALERT")
        for oid, d_ in sorted(new, key=lambda x: x[1]):
            key = (oid, d_)
            if key in seen:
                continue
            seen.add(key)
            msg = f"{W.office_label(oid)} {W.parse_date(d_)} " \
                  f"[{W.STATE_CN.get(slots[key], '')}]"
            log(f"     {msg}", "ALERT")
            print(f"\n{'!'*62}\n  ★ {msg}\n  立即预约: {W.BOOKING_URL}\n{'!'*62}\n",
                  flush=True)

        if not quiet:
            try:
                first = sorted(new, key=lambda x: x[1])[0]
                notifier.notify(
                    "香港身份证有名额！",
                    f"新增 {len(new)} 个\n"
                    f"{W.office_label(first[0])} {W.parse_date(first[1])}",
                    sound=True)
            except Exception as e:
                log(f"通知失败: {e}", "WARN")
        if not no_open:
            try:
                webbrowser.open(W.BOOKING_URL)
            except Exception:
                pass

        prev_fp, prev_slots = fp, dict(slots)
        time.sleep(10 if new else iv)      # 有新增后短暂高频跟进


def main():
    ap = argparse.ArgumentParser(description="时段感知监控（依据实测放号节律）")
    ap.add_argument("--office", nargs="*", help="只盯这些办事处，如 九龙 / RKO")
    ap.add_argument("--within", type=int, default=None, help="只看未来 N 天")
    ap.add_argument("--min-state", type=int, default=2, help="2=少量 3=充足")
    ap.add_argument("--always-fast", action="store_true", help="全天候 10 秒")
    ap.add_argument("--no-open", action="store_true", help="不自动开网页")
    ap.add_argument("--quiet", action="store_true", help="不弹窗")
    ap.add_argument("--minutes", type=float, default=0, help="跑 N 分钟(0=不限)")
    ap.add_argument("--show-plan", action="store_true", help="只打印时段计划")
    args = ap.parse_args()

    if args.show_plan:
        show_plan()
        return 0

    offices = None
    if args.office:
        offices = set()
        for o in args.office:
            k = o.strip().upper()
            if k in W.OFFICE_ALIASES:
                offices.add(W.OFFICE_ALIASES[k])
            elif k in W.OFFICE_NAMES:
                offices.add(k)
            else:
                print(f"错误: 无法识别办事处 {o}", file=sys.stderr)
                return 2

    log("=" * 62)
    log("时段感知监控启动（09:00-12:00 为实测放号高峰）")
    log(f"办事处: {', '.join(W.office_label(o) for o in offices) if offices else '全部'}")
    log(f"模式: {'全天候 10 秒' if args.always_fast else '按实测节律自适应'}")
    log(f"弹窗: {'关' if args.quiet else '开'}  自动开页: {'关' if args.no_open else '开'}")
    log("=" * 62)
    # 横幅已在 main 打印过，run() 里不再重复
    run(offices, args.within, args.min_state, args.always_fast,
        args.no_open, args.quiet, args.minutes, banner=False)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已退出")
        sys.exit(0)
