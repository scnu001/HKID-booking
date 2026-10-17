#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
香港身份证预约配额监测器  (HKID Appointment Quota Watcher)
==========================================================

数据来源：香港入境处官方「各人事登记办事处预约配额预览」公开接口
  https://eservices.es2.immd.gov.hk/surgecontrolgate/ticket/getSituation?svcId=579

该接口就是官方预览页 (www.gov.hk/sc/apps/bookidcardquota.htm) 背后的真实数据源，
无需登录、无需 Cookie、无验证码，纯 GET 请求。官方页面自己也是 15 分钟刷一次。

配额字段含义 (取自官方 style.css)：
  quota-g = 尚有名额 (Quota Available)
  quota-y = 少量名额 (Limited Quota)
  quota-r = 已满     (Full)

用法：
  python hkid_watch.py                     # 默认每 3 分钟查一次
  python hkid_watch.py --interval 60       # 每 60 秒查一次
  python hkid_watch.py --once              # 只查一次，打印当前全部配额
  python hkid_watch.py --office 港岛 九龙   # 只盯指定办事处
  python hkid_watch.py --within 30         # 只关心 30 天内（约 22 个工作天）
  python hkid_watch.py --open-on-hit       # 一有票就自动打开浏览器

合规提示：
  入境处条款明确禁止用自动化工具干扰预约系统。本脚本只做「只读查询 + 通知」，
  不提交预约、不绕过验证码、不并发轰炸。请用它来「第一时间知道有票，然后自己去点」，
  而不是用来绕过排队。这既是你自己的成功率最大化，也不伤害其他人。
"""

import argparse
import json
import os
import sys
import time
import webbrowser
from datetime import datetime, timedelta

# ---------------------------------------------------------------- 配置

QUOTA_URL = ("https://eservices.es2.immd.gov.hk/surgecontrolgate/ticket/getSituation"
             "?svcId=579&t={ts}")

# 官方预约入口。
#
# ⚠️ 不能用 webapp.../eservices/indexPage?svcId=579 这种「深链」：
#    服务端虽返回 200，但那是 SPA 空壳；前端路由发现没有 surge-control
#    票据，直接渲染「错误代码 当前页面找不到」= 用户看到 404。
#
# ✅ 正确入口是**票据网关**：先拿一次性 ticketId，再 302 到真正的预约页。
#    https://eservices.es2.immd.gov.hk/surgecontrolgate/ticket/getTicketGet?svcId=579&language=zh&country=HK
#    每次点都换新票据，所以这个 URL 可以重复使用。
BOOKING_URL = "https://www.gov.hk/sc/apps/immdicbooking2.htm"

# 同一壳页的繁体版（country 会变成 HK，界面是繁体）
BOOKING_URL_TC = "https://www.gov.hk/tc/apps/immdicbooking2.htm"

# 壳页里 immdicbooking2.js 的跳转目标（2024-05 版），供「直连」备用。
# 注意三个都跟旧版不一样：域名 entry / 路径 surgecontrol_extend / svcId=57A
BOOKING_URL_DIRECT = ("https://entry.es2.immd.gov.hk/surgecontrol_extend/"
                      "ticket/getTicketGet?svcId=57A&language=zh&country=CN")

# 旧版网关（svcId=579）。它现在 307 跳维护页 —— 是**维护公告的唯一来源**，
# 因为新版 57A 被 Incapsula WAF 挡着，脚本读不到内容。
BOOKING_URL_LEGACY = ("https://eservices.es2.immd.gov.hk/surgecontrolgate/"
                      "ticket/getTicketGet?svcId=579&language=zh&country=HK")

# 站内功能页（**必须带 ticket，直接访问一律 404**）。仅用于提示文案。
BOOKING_PAGES = {
    "new": ("预约", "预约"),
    "change": ("更改／取消预约", "更改／取消预约"),
    "enquire": ("查询预约", "查询预约"),
    "reminder": ("更改个人预约提示", "更改个人预约提示"),
}

# 备用：走 gov.hk 官方介绍页（壳页也挂了时的兜底）
BOOKING_FALLBACK = ("https://www.gov.hk/tc/residents/immigration/"
                    "idcard/hkic/bookregidcard.htm")

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

OFFICE_NAMES = {
    "RHK": "港岛办事处", "RKO": "九龙办事处", "RTK": "将军澳办事处",
    "FTO": "火炭办事处", "TMO": "屯门办事处", "YLO": "元朗办事处",
}

# 允许用户用简称：港岛 / 港岛办事处 / RHK 都认
OFFICE_ALIASES = {}
for _oid, _nm in OFFICE_NAMES.items():
    OFFICE_ALIASES[_oid.upper()] = _oid
    OFFICE_ALIASES[_nm] = _oid
    OFFICE_ALIASES[_nm.replace("办事处", "")] = _oid
# 额外别名
OFFICE_ALIASES.update({
    "港島": "RHK", "港島辦事處": "RHK", "香港島": "RHK",
    "九龍": "RKO", "九龍辦事處": "RKO",
    "將軍澳": "RTK", "将军澳办": "RTK",
    "火炭辦事處": "FTO", "屯門": "TMO", "屯門辦事處": "TMO",
    "元朗辦事處": "YLO",
})

# 状态等级：数字越大越有机会
STATE_RANK = {"quota-g": 3, "quota-y": 2, "quota-r": 1, "quota-non": 0}
STATE_CN = {"quota-g": "尚有名额", "quota-y": "少量名额",
            "quota-r": "已满", "quota-non": "不适用"}

# 接口的 col 是 ["date","quotaR","quotaK"]，对应两个时段大类：
#   quotaR = 一般服务时段（General Service Hours）
#   quotaK = 延长服务时段（Extended Service Hours），"no-quotaK" = 当天不设延长时段
# ⚠️ 接口**没有具体钟点**（09:30/10:00 那种），只有这两类。
#    具体时段要进预约流程选完日期才显示，需 ticket + 验证码，脚本拿不到。
HOURS_CN = {"regular": "一般时段", "extended": "延长时段"}
HOURS_COL = {"regular": "quotaR", "extended": "quotaK"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


# ---------------------------------------------------------------- 工具

def log(msg, level="INFO", logfile=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {level:<5} {msg}"
    print(line, flush=True)
    if logfile:
        try:
            with open(logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def office_label(oid):
    return OFFICE_NAMES.get(oid, oid)


def parse_date(s):
    """接口日期格式 MM/DD/YYYY -> date"""
    try:
        m, d, y = s.split("/")
        return datetime(int(y), int(m), int(d)).date()
    except Exception:
        return None


# ---------------------------------------------------------------- 拉取

def fetch_quota(timeout=25):
    """拉取一次配额。返回 (状态码, 数据dict 或 None)。"""
    import urllib.request
    import urllib.error

    url = QUOTA_URL.format(ts=int(time.time() * 1000))
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": ("https://eservices.es2.immd.gov.hk/es/"
                    "quota-enquiry-client/?l=zh-CN&appId=579"),
        "Cache-Control": "no-cache",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.status != 200 or not raw:
                return resp.status, None
            return resp.status, json.loads(raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return -1, {"__error__": f"{type(e).__name__}: {e}"}


def parse_quota(data):
    """把接口 JSON 摊平成 {(officeId, dateStr): state}

    ⚠️ 注意：这个函数把「一般服务时段(quotaR)」和「延长服务时段(quotaK)」
    合并成「取较好的一个」，**丢掉了时段维度**。
    要分时段判断必须用 parse_quota_detail()。
    """
    if not data or "__error__" in data:
        return {}, {}
    offices = {}
    for o in data.get("office", []):
        nm = (o.get("chs") or {}).get("officeName") or (o.get("cht") or {}).get("officeName")
        offices[o.get("officeId")] = nm or o.get("officeId")

    grid = {}
    for row in data.get("data", []):
        key = (row.get("officeId"), row.get("date"))
        # quotaR = 一般服务时段；quotaK = 延长服务时段。取较高的那个
        r = row.get("quotaR", "quota-non")
        k = row.get("quotaK", "quota-non")
        grid[key] = r if STATE_RANK.get(r, 0) >= STATE_RANK.get(k, 0) else k
    return grid, offices


def parse_quota_detail(data):
    """保留两个时段维度的版本。

    返回 (offices, detail)，detail = {(officeId, dateStr): (quotaR, quotaK)}

    为什么需要它：接口的 col 是 ["date","quotaR","quotaK"]
      · quotaR = 一般服务时段（General Service Hours）
      · quotaK = 延长服务时段（Extended Service Hours），值为 "no-quotaK" 表示当天没有
    合并成单一状态会漏报——例如某天 R 本来就有票、K 从「无」变成「有」，
    合并后取 max 结果不变，就永远不会触发提醒。
    """
    if not data or "__error__" in data:
        return {}, {}
    offices = {}
    for o in data.get("office", []):
        nm = (o.get("chs") or {}).get("officeName") or (o.get("cht") or {}).get("officeName")
        offices[o.get("officeId")] = nm or o.get("officeId")

    detail = {}
    for row in data.get("data", []):
        key = (row.get("officeId"), row.get("date"))
        detail[key] = (row.get("quotaR", "quota-non"),
                       row.get("quotaK", "no-quotaK"))
    return offices, detail


# ---------------------------------------------------------------- 核心

def summarize(grid, offices, only_offices=None, within_days=None, min_state=2):
    """按办事处汇总当前值得关注的名额"""
    today = datetime.now().date()
    cutoff = today + timedelta(days=within_days) if within_days else None

    out = []
    for (oid, datestr), state in grid.items():
        if only_offices and oid not in only_offices:
            continue
        if STATE_RANK.get(state, 0) < min_state:
            continue
        d = parse_date(datestr)
        if cutoff and d and d > cutoff:
            continue
        out.append((oid, d, datestr, state))
    # 排序：有名额优先 → 日期近的优先
    out.sort(key=lambda x: (-STATE_RANK.get(x[3], 0),
                            x[1] or datetime.max.date()))
    return out


def print_table(grid, offices, only_offices=None, within_days=None):
    """打印完整网格：一眼看全所有办事处 × 日期状态"""
    today = datetime.now().date()
    cutoff = today + timedelta(days=within_days) if within_days else None

    filtered = {}
    for (oid, datestr), state in grid.items():
        if only_offices and oid not in only_offices:
            continue
        d = parse_date(datestr)
        if cutoff and d and d > cutoff:
            continue
        filtered[(oid, datestr)] = state

    by_office = {}
    for (oid, datestr), state in filtered.items():
        by_office.setdefault(oid, {})[datestr] = state

    oids = [o for o in (only_offices or sorted(by_office)) if o in by_office]
    dates = sorted({d for o in oids for d in by_office[o]},
                   key=lambda s: parse_date(s) or datetime.max.date())

    ICON = {"quota-g": "G", "quota-y": "Y", "quota-r": ".", "quota-non": "-"}
    print("\n  G=尚有名额  Y=少量名额  .=已满  (每列=一个日期, 括号内为月/日)")
    print("  " + " " * 12 + "".join(f"{parse_date(d).strftime('(%m/%d)'):>9}"
                                       for d in dates[:20]))
    for oid in oids:
        name = offices.get(oid) or office_label(oid)
        cells = "".join(f"{ICON.get(by_office[oid].get(d, 'quota-non'), '?'):>9}"
                        for d in dates[:20])
        print(f"  {name:<10}{cells}")
    if len(dates) > 20:
        print(f"  ... 共 {len(dates)} 个日期, 以上为最近 20 个")


def main():
    ap = argparse.ArgumentParser(
        description="香港身份证预约配额监测器 (只读查询, 不自动提交)")
    ap.add_argument("--interval", type=int, default=180, help="轮询间隔秒数 (默认180)")
    ap.add_argument("--once", action="store_true", help="只查一次并打印全部配额后退出")
    ap.add_argument("--office", nargs="*", help="只盯这些办事处, 如: 港岛 九龙 火炭")
    ap.add_argument("--within", type=int, default=None,
                    help="只关心未来 N 天内的名额 (默认全部96个工作天)")
    ap.add_argument("--min-state", type=int, default=2,
                    help="最低关注等级 1=已满 2=少量 3=充足 (默认2)")
    ap.add_argument("--open-on-hit", action="store_true",
                    help="一有票就自动打开官方预约页面")
    ap.add_argument("--notify", action="store_true",
                    help="一有票就弹 Windows 桌面通知 + 响一声")
    ap.add_argument("--notify-always", action="store_true",
                    help="每次发现名额都通知(默认只在状态变好时通知一次)")
    args = ap.parse_args()

    # 支持中文名、简称、繁体、英文代号；认不出来就直接报错，别静默跑出 0 个
    only = None
    if args.office:
        only = set()
        unknown = []
        for o in args.office:
            key = o.strip().upper()
            if key in OFFICE_ALIASES:
                only.add(OFFICE_ALIASES[key])
            elif key in OFFICE_NAMES:
                only.add(key)
            else:
                unknown.append(o)
        if unknown:
            valid = "、".join(f"{v}({k})" for k, v in OFFICE_NAMES.items())
            print(f"错误: 无法识别的办事处 {unknown}\n可选: {valid}", file=sys.stderr)
            return 2
        if not only:
            print("错误: --office 没有有效值", file=sys.stderr)
            return 2

    os.makedirs(LOG_DIR, exist_ok=True)
    logfile = os.path.join(LOG_DIR, f"watch-{datetime.now():%Y%m%d}.log")
    statefile = os.path.join(LOG_DIR, "last_state.json")

    log("=" * 62)
    log(f"香港身份证预约配额监测器启动  间隔={args.interval}s")
    if only:
        log(f"只盯: {', '.join(office_label(o) for o in only)}")
    if args.within:
        log(f"范围: 未来 {args.within} 天内")
    log(f"日志: {logfile}")
    log("=" * 62)

    prev = {}
    if os.path.exists(statefile):
        try:
            with open(statefile, encoding="utf-8") as f:
                prev = {tuple(json.loads(k)): v for k, v in json.load(f).items()}
        except Exception:
            prev = {}

    consecutive_errors = 0
    hits_logged = set()

    while True:
        try:
            status, data = fetch_quota()

            if status != 200 or not data or "__error__" in (data or {}):
                consecutive_errors += 1
                err = (data or {}).get("__error__", f"HTTP {status}")
                log(f"查询失败 ({err}) — 连续第 {consecutive_errors} 次", "WARN", logfile)
                if args.once:
                    return 1
                time.sleep(min(60, 5 * consecutive_errors))
                continue

            consecutive_errors = 0
            grid, offices = parse_quota(data)
            updated = data.get("lastUpdateTime", "?")
            log(f"拉取成功 | 官方数据时间 {updated} | {len(grid)} 条记录", "INFO", logfile)

            if args.once:
                print_table(grid, offices, only, args.within)
                found = summarize(grid, offices, only, args.within, args.min_state)
                print(f"\n符合条件的名额: {len(found)} 个")
                for oid, d, ds, st in found[:25]:
                    print(f"  {office_label(oid):<8} {d}  [{STATE_CN.get(st, st)}]")
                return 0

            # ---- 比对：找出「从没有变成有」的新名额 ----
            fresh = []
            for (oid, datestr), state in grid.items():
                if STATE_RANK.get(state, 0) < args.min_state:
                    continue
                if only and oid not in only:
                    continue
                d = parse_date(datestr)
                if args.within and d and d > datetime.now().date() + timedelta(days=args.within):
                    continue
                old = prev.get((oid, datestr))
                if old != state:
                    tag = f"{oid}|{datestr}"
                    # 已满 -> 有票 才算「新票」；首次运行时只播报少量名额提醒
                    if old is None:
                        if STATE_RANK.get(state, 0) == 3 and prev:
                            fresh.append((oid, d, datestr, state, "首次发现充足名额"))
                        elif not prev and STATE_RANK.get(state, 0) == 2 and len(hits_logged) < 5:
                            fresh.append((oid, d, datestr, state, "当前少量名额(参考)"))
                    elif STATE_RANK.get(old, 0) < STATE_RANK.get(state, 0):
                        fresh.append((oid, d, datestr, state,
                                      f"{STATE_CN.get(old, old)} → {STATE_CN.get(state, state)}"))

            for oid, d, datestr, state, why in fresh:
                msg = (f"★ 新名额! {office_label(oid)} {d} "
                       f"[{STATE_CN.get(state, state)}] ({why})")
                log(msg, "ALERT", logfile)
                print("\n" + "!" * 62)
                print(f"  {msg}")
                print(f"  立即预约: {BOOKING_URL}")
                print("!" * 62 + "\n", flush=True)
                # 桌面通知 + 提示音（best-effort，失败不影响主流程）
                if args.notify:
                    try:
                        import notify as notifier
                        notifier.notify("香港身份证预约有名额",
                                        f"{office_label(oid)} {d} "
                                        f"{STATE_CN.get(state, state)}", sound=True)
                    except Exception as e:
                        log(f"桌面通知失败: {e}", "WARN", logfile)
                if args.open_on_hit:
                    try:
                        webbrowser.open(BOOKING_URL)
                    except Exception:
                        pass
                hits_logged.add(f"{oid}|{datestr}")

            # 写状态
            try:
                with open(statefile, "w", encoding="utf-8") as f:
                    json.dump({json.dumps(list(k)): v for k, v in grid.items()}, f)
                prev = {k: v for k, v in grid.items()}
            except OSError:
                pass

            now_hits = summarize(grid, offices, only, args.within, args.min_state)
            log(f"当前符合条件名额: {len(now_hits)} 个 | 下次检查 {args.interval}s 后",
                "INFO", logfile)

        except KeyboardInterrupt:
            log("手动停止", "INFO", logfile)
            return 0
        except Exception as e:
            log(f"未预期错误: {e}", "ERROR", logfile)

        if args.once:
            return 0
        # 分片 sleep，让 Ctrl+C 响应更快
        slept = 0
        while slept < args.interval:
            time.sleep(min(1, args.interval - slept))
            slept += 1
            if slept % 10 == 0:
                pass


if __name__ == "__main__":
    import urllib.request  # noqa: F401  (urllib_request 内部导入用)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已退出")
        sys.exit(0)
