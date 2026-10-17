# -*- coding: utf-8 -*-
"""
香港身份证预约监控 —— 可视化控制台后台

只依赖 Python 标准库（http.server + json），无需 pip install。
启动后浏览器打开 http://127.0.0.1:8770

提供 API：
  GET  /api/state        当前状态（配额快照 + 配置 + 运行信息）
  GET  /api/config       读配置
  POST /api/config       存配置
  POST /api/refresh      立刻刷新一次
  POST /api/test-notify  测试提醒
  GET  /api/history      历史放号事件
  POST /api/reset        清除基线（重新开始盯）
  POST /api/open-booking 打开官方预约页（服务端唤起默认浏览器）

「指定日期必盯」：配置 focus_dates（MM/DD/YYYY），这些日期
  · 无视 within_days 时间范围
  · 无视 min_state 门槛（少量名额也报）
  · 越临近轮询越密（<=1天 3s / <=3天 5s / <=7天 8s / <=14天 10s）
"""
import json
import os
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import hkid_watch as W
import notify as notifier

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
HISTORY_PATH = os.path.join(HERE, "logs", "history.json")
LOG_DIR = os.path.join(HERE, "logs")
# 预约资料备忘：**明文存本机**，绝不上传。放在脚本目录
PROFILE_PATH = os.path.join(HERE, "profile.json")
os.makedirs(LOG_DIR, exist_ok=True)

PORT = 8770

# ---------------------------------------------------------------- 配置

DEFAULT_CONFIG = {
    "offices": ["RKO"],                  # 要盯的办事处
    "within_days": 90,                   # 只关心未来 N 天，0 = 全部
    "min_state": 2,                      # 2=少量名额也报, 3=只报充足
    "focus_dates": [],                   # 指定日期必盯( MM/DD/YYYY )，无视范围与门槛
    "service_hours": "any",              # any=两种时段都报 / regular / extended
    "alert_gap": 90,                     # 两次提醒的最小间隔(秒)，防放号抖动刷屏
    "interval": 10,                      # 轮询间隔(秒)
    "fixed_interval": True,              # True=锁死不退避，False=无变化时自动放宽
    "notify": {
        "desktop": True,                 # Windows 弹窗
        "sound": True,                   # 提示音
        "open_browser": True,            # 自动打开预约页
        "only_when_new": True,           # 只在"新增名额"时提醒(不重复刷屏)
    },
    "quiet_hours": {"enabled": False, "from": "23:00", "to": "08:00"},
    "auto_start": True,                  # 启动后立即开始监控
}

_config_lock = threading.Lock()
_state_lock = threading.Lock()
_maint_cache = {}

STATE = {
    "running": False,
    "started_at": None,
    "last_check": None,
    "last_error": None,
    "next_check_in": None,
    "backoff_idx": 0,
    "polls": 0,
    "fingerprint": None,
    "baseline_set": False,
    "quota": {},          # {officeId: {date: state}} 合并视图
    "quota_r": {},        # 一般服务时段
    "quota_k": {},        # 延长服务时段
    "official_time": None,
    "recent": [],         # 最近事件
    "focus_reported": {}, # {(office|date|hours): state} 指定日已提醒过的状态
    "focus_last": {},     # 同上 key -> 上次提醒时间戳（60 秒去抖用）
    "last_sound_ts": 0.0, # 上次响铃时间（声音限流用）
    "notification_test": None,
}

STOP = threading.Event()


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {level:<5} {msg}", flush=True)
    lg = os.path.join(LOG_DIR, f"console-{datetime.now():%Y%m%d}.log")
    try:
        with open(lg, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {level:<5} {msg}\n")
    except OSError:
        pass


def load_config():
    with _config_lock:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    cfg = json.load(f)
                # 补齐缺失字段
                for k, v in DEFAULT_CONFIG.items():
                    cfg.setdefault(k, v)
                for k, v in DEFAULT_CONFIG["notify"].items():
                    cfg["notify"].setdefault(k, v)
                for k, v in DEFAULT_CONFIG["quiet_hours"].items():
                    cfg["quiet_hours"].setdefault(k, v)
                return cfg
            except Exception as e:
                log(f"配置读取失败, 用默认值: {e}", "WARN")
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg):
    with _config_lock:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    log(f"配置已保存: 办事处={cfg['offices']} 范围={cfg['within_days']}天 "
        f"提醒={ {k: v for k, v in cfg['notify'].items() if v} }")


def load_history():
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_history(h):
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(h[-500:], f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------- 预约资料备忘
#
# 字段完全对齐入境处官方流程（pointToNote）：
#   申请类别 -> 证明文件类别 -> 证明文件号码 -> 出生年份/月/日
#   -> 4 位查询编号 -> 办事处/日期/时段 -> 电邮
#
# ⚠️ 安全：这些是**身份证明文件号码**级敏感信息。只写本机 profile.json，
#    不联网、不写日志、不进 state 的公开字段。删除用 DELETE /api/profile。

PROFILE_FIELDS = [
    "category",       # 申请类别
    "doc_type",       # 证明文件类别
    "doc_no",         # 证明文件号码
    "permit_last6",   # ★学生：港澳通行证后 6 位（官方只要后 6 位）
    "evisa_ref",      # ★学生：学生 e-Visa「签证/进入许可申请档案编号」
    "birth_y", "birth_m", "birth_d",
    "query_code",     # 4 位自选查询编号
    "email",
    "name_note",      # 姓名备注（官方不收集，纯粹让你自己认人）
    "applicants",     # 同行人数 1-4
    "notes",          # 其他备忘
]

# 官方表单只收「出生日 + 出生年份」，**没有月份**。月份仅作个人备忘。
PROFILE_CLEAN = {
    # 字段名: (去除用的正则, 最大长度, 是否转大写)
    # 注意 permit_last6 不在这里 —— 它需要「取后 6 位」而不是「取前 6 位」，
    # 在 save_profile 里单独处理。
    "query_code": (r"\D", 4, False),
}


def _clean_field(key, val):
    """按 PROFILE_CLEAN 规则清洗；未列出的字段只做基本裁剪。"""
    rule = PROFILE_CLEAN.get(key)
    if not rule:
        return val[:200]
    pattern, maxlen, upper = rule
    v = re.sub(pattern, "", val)
    if upper:
        v = v.upper()
    return v[:maxlen]


def load_profile():
    """读资料备忘。文件不存在返回空结构（不写盘）。"""
    empty = {k: "" for k in PROFILE_FIELDS}
    empty["applicants"] = "1"
    empty["_updated"] = None
    if not os.path.exists(PROFILE_PATH):
        return empty
    try:
        with open(PROFILE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return empty
        out = {k: str(d.get(k, "") or "") for k in PROFILE_FIELDS}
        out["applicants"] = str(d.get("applicants") or "1")
        out["_updated"] = d.get("_updated")
        return out
    except Exception as e:
        log(f"资料读取失败: {type(e).__name__}: {e}", "WARN")
        return empty


def save_profile(data):
    """只接白名单字段，全转成字符串，长度截断。"""
    cur = load_profile()
    for k in PROFILE_FIELDS:
        if k in data:
            v = data[k]
            if not isinstance(v, (str, int, float)):
                v = ""
            cur[k] = str(v)[:200]
    try:
        n = int(cur.get("applicants") or 1)
    except (TypeError, ValueError):
        n = 1
    cur["applicants"] = str(max(1, min(4, n)))    # 官方上限 4 人
    # 查询编号、通行证后 6 位统一清洗（e-Visa 编号常有空格/换行，粘贴后必清）
    cur["query_code"] = _clean_field("query_code", cur.get("query_code") or "")
    # 港澳通行证：官方要「最后六位**数字**」。用户常把完整号码
    # （如 C12345678）整串粘进来 —— 必须取数字部分的**后** 6 位，
    # 取前 6 位会得到 C+5 位数字，完全错误。
    permit = re.sub(r"\D", "", cur.get("permit_last6") or "")
    cur["permit_last6"] = permit[-6:] if len(permit) > 6 else permit
    # e-Visa 档案编号：去掉所有空白（复制 PDF 常带换行），保留连字符
    cur["evisa_ref"] = re.sub(r"\s+", "", cur.get("evisa_ref") or "")[:40]
    cur["_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tmp = PROFILE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROFILE_PATH)
    # 日志里绝不打印内容，只记「存了」
    log("预约资料已保存（内容不写日志）")
    return cur


def delete_profile():
    try:
        if os.path.exists(PROFILE_PATH):
            os.remove(PROFILE_PATH)
    except OSError as e:
        log(f"删除资料失败: {e}", "WARN")
        return False
    log("预约资料已清除")
    return True


# ---------------------------------------------------------------- 核心逻辑

def fingerprint(quota_r, quota_k, offices):
    """内容指纹。**必须把两个时段都算进去**。

    只算合并状态会漏报：某天一般时段本来就 quota-g、延长时段从
    no-quotaK 变成 quota-g 时，合并取值不变 → 指纹不变 → 永远不提醒。
    """
    s = "".join(f"{o}{d}{quota_r[o][d]}/{quota_k[o][d]}"
                for o in sorted(offices) for d in sorted(quota_r.get(o, {})))
    import hashlib
    return hashlib.md5(s.encode()).hexdigest()[:12]


def poll_once(cfg, quiet=False):
    """拉一次数据、更新状态、检测变化、必要时提醒。返回新增列表。"""
    st, d = W.fetch_quota()
    now = datetime.now()
    with _state_lock:
        STATE["polls"] += 1
        STATE["last_check"] = now.strftime("%Y-%m-%d %H:%M:%S")

    if st != 200 or not d:
        with _state_lock:
            STATE["last_error"] = f"HTTP {st}"
        if not quiet:
            log(f"查询失败 HTTP {st}", "WARN")
        return []

    offices, detail = W.parse_quota_detail(d)

    # 拆成三个视图：
    #   quota     合并(取较好)，兼容旧逻辑/热力图默认视图
    #   quota_r   只看一般服务时段
    #   quota_k   只看延长服务时段
    quota, quota_r, quota_k = {}, {}, {}
    for (oid, ds), (r, k) in detail.items():
        best = r if W.STATE_RANK.get(r, 0) >= W.STATE_RANK.get(k, 0) else k
        quota.setdefault(oid, {})[ds] = best
        quota_r.setdefault(oid, {})[ds] = r
        quota_k.setdefault(oid, {})[ds] = k

    offices_cfg = cfg["offices"]
    fp = fingerprint(quota_r, quota_k, offices_cfg)

    with _state_lock:
        prev_fp = STATE["fingerprint"]
        prev_r = {o: dict(quota_r.get(o, {})) for o in offices_cfg}
        prev_k = {o: dict(quota_k.get(o, {})) for o in offices_cfg}
        STATE["quota"] = quota
        STATE["quota_r"] = quota_r
        STATE["quota_k"] = quota_k
        STATE["official_time"] = d.get("lastUpdateTime")
        STATE["last_error"] = None
        STATE["fingerprint"] = fp

    # ---- A. 指定日期：**有号就报**，不依赖「状态变化」 ----
    #
    # 原先指定日期也走「状态变了才报」的逻辑，导致一个致命问题：
    # 用户设了 10/30，而 10/30 本来就是 quota-y 且一直没变 —— 永远不提醒。
    # 用户要的语义是「我指定的那天有号就告诉我」，不是「变了才告诉我」。
    #
    # 所以单独扫描：当前 rank>=2 且「这个状态还没报过」就报。
    # 用 STATE["focus_reported"] 记住已报过的状态，避免每轮刷屏；
    # 状态变好（y→g）会再报一次；变成没号则清标记，下次有号重新报。
    focus = norm_focus(cfg)                    # {date: "MM/DD/YYYY"}
    focus_ord = set(d.toordinal() for d in focus)
    svc = cfg.get("service_hours", "any")      # any/regular/extended

    new_items = []
    seen = set()                               # 去重：(office,date,hours)

    with _state_lock:
        reported = dict(STATE.get("focus_reported") or {})
        focus_last = dict(STATE.get("focus_last") or {})

    if focus_ord:
        now_ts = now.timestamp()
        for oid in offices_cfg:
            cr = quota_r.get(oid, {})
            ck = quota_k.get(oid, {})
            for ds in cr:
                dt = W.parse_date(ds)
                if not dt or dt.toordinal() not in focus_ord:
                    continue
                for hours, cur_s in (("regular", cr.get(ds)),
                                     ("extended", ck.get(ds))):
                    if svc == "regular" and hours != "regular":
                        continue
                    if svc == "extended" and hours != "extended":
                        continue
                    key = f"{oid}|{ds}|{hours}"
                    rank = W.STATE_RANK.get(cur_s, 0)
                    if rank < 2:               # 没号/已满 -> 清标记，等下次
                        reported.pop(key, None)
                        continue
                    # 状态没变 -> 不报（避免每轮重复）
                    if reported.get(key) == cur_s:
                        continue
                    reported[key] = cur_s
                    # 放号后名额会在有/无之间反复闪（有人抢有人退），
                    # 60 秒内同一个「办事处+日期+时段」最多提醒一次，
                    # 否则会连续弹好几次，反而让人以为是卡了。
                    if now_ts - focus_last.get(key, 0) < 60:
                        continue
                    focus_last[key] = now_ts
                    seen.add((oid, ds, hours))
                    new_items.append({
                        "office": oid,
                        "officeName": W.OFFICE_NAMES.get(oid, oid),
                        "date": ds,
                        "dateObj": dt.strftime("%Y-%m-%d"),
                        "state": cur_s,
                        "stateCn": W.STATE_CN.get(cur_s, cur_s),
                        "hours": hours,
                        "hoursCn": W.HOURS_CN.get(hours, hours),
                        "focus": True,
                    })
        with _state_lock:
            STATE["focus_reported"] = reported
            STATE["focus_last"] = focus_last

    # ---- 首次只建基线（普通日期的「变化」不比，但上面的指定日已经报过了） ----
    if prev_fp is None:
        with _state_lock:
            STATE["baseline_set"] = True
        if not quiet:
            log(f"基线建立 指纹={fp} 官方数据时间={d.get('lastUpdateTime')}")
        if new_items:
            return _emit(cfg, new_items, now, prev_fp, fp, quiet=True)
        return []

    if prev_fp != fp:
        # ---- B. 指纹变了：逐办事处、逐时段找新增 ----
        min_rank = cfg["min_state"]
        within = cfg["within_days"]
        cutoff = (now.date() + timedelta(days=within)).toordinal() \
                 if within else None

        for oid in offices_cfg:
            cr = quota_r.get(oid, {})
            ck = quota_k.get(oid, {})
            orr = prev_r.get(oid, {})
            okk = prev_k.get(oid, {})
            for ds in cr:
                dt = W.parse_date(ds)
                is_focus = bool(dt and dt.toordinal() in focus_ord)

                # 分别比较两个时段，哪个有变化就报哪个（合并会漏报）
                for hours, cur_s, old_s in (("regular", cr.get(ds), orr.get(ds)),
                                            ("extended", ck.get(ds), okk.get(ds))):
                    if svc == "regular" and hours != "regular":
                        continue
                    if svc == "extended" and hours != "extended":
                        continue
                    if cur_s == old_s:
                        continue
                    rank = W.STATE_RANK.get(cur_s, 0)
                    if is_focus:
                        # 指定日已在 A 段按「有号就报」处理过，这里避免重复
                        if (oid, ds, hours) in seen:
                            continue
                        if rank < 2:
                            continue
                    else:
                        if rank < min_rank:
                            continue
                        if cutoff and dt and dt.toordinal() > cutoff:
                            continue
                    new_items.append({
                        "office": oid,
                        "officeName": W.OFFICE_NAMES.get(oid, oid),
                        "date": ds,
                        "dateObj": dt.strftime("%Y-%m-%d") if dt else ds,
                        "state": cur_s,
                        "stateCn": W.STATE_CN.get(cur_s, cur_s),
                        "hours": hours,
                        "hoursCn": W.HOURS_CN.get(hours, hours),
                        "focus": is_focus,
                    })

    if not new_items:
        return []

    return _emit(cfg, new_items, now, prev_fp, fp, quiet=quiet)


def _emit(cfg, new_items, now, prev_fp=None, fp=None, quiet=False):
    """记录事件 + 发提醒。抽出来是因为指定日期在「首次建基线」时也可能要报。"""
    new_items.sort(key=lambda x: (x["dateObj"], x["office"]))
    event = {
        "at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(new_items),
        "items": new_items,
    }
    with _state_lock:
        STATE["recent"].insert(0, event)
        STATE["recent"] = STATE["recent"][:50]

    hist = load_history()
    hist.insert(0, event)
    save_history(hist)

    focus_n = sum(1 for x in new_items if x.get("focus"))
    tag = f"（其中 {focus_n} 个是★指定日）" if focus_n else ""
    log(f"★★ 检测到 {len(new_items)} 个名额{tag} "
        f"(指纹 {prev_fp} -> {fp})", "ALERT")
    for it in new_items[:8]:
        star = "★" if it.get("focus") else " "
        log(f"    {star} {it['officeName']} {it['dateObj']} "
            f"[{it['hoursCn']}·{it['stateCn']}]", "ALERT")

    # 提醒
    if not in_quiet_hours(cfg):
        n = cfg["notify"]
        if n.get("desktop"):
            try:
                # ---- 内容签名：内容完全一样就不重复弹 ----
                # 放号后名额会在有/无之间反复抖，同一批日期可能被反复检测到。
                # 用「办事处+日期+时段+状态」排序后的 MD5 作为签名，
                # 和上次弹过的相同且在 alert_gap 秒内 -> 直接跳过。
                import hashlib
                sig_src = "|".join(sorted(
                    f"{it['office']}{it['dateObj']}{it['hours']}{it['state']}"
                    for it in new_items))
                sig = hashlib.md5(sig_src.encode()).hexdigest()[:16]
                gap = cfg.get("alert_gap", 90)
                with _state_lock:
                    last_sig = STATE.get("last_alert_sig")
                    last_ts = STATE.get("last_alert_ts", 0.0)
                if sig == last_sig and (time.time() - last_ts) < gap:
                    log(f"内容与上次相同（{sig}），{gap}s 内不重复弹窗", "INFO")
                    with _state_lock:
                        STATE["notification_test"] = "内容相同，已跳过"
                    return new_items
                with _state_lock:
                    STATE["last_alert_sig"] = sig
                    STATE["last_alert_ts"] = time.time()

                first = new_items[0]
                star = "★指定日 " if first.get("focus") else ""
                title = ("★ 指定日期有名额！" if first.get("focus")
                         else "香港身份证有名额！")
                body = (f"{len(new_items)} 个名额\n"
                        f"{star}{first['officeName']} {first['dateObj']} "
                        f"（{first['hoursCn']}·{first['stateCn']}）\n")
                # 多于一条时，把其余几条也列出来
                if len(new_items) > 1:
                    for it in new_items[1:6]:
                        body += (f"　· {it['officeName']} {it['dateObj']} "
                                 f"{it['hoursCn']}·{it['stateCn']}\n")
                    if len(new_items) > 6:
                        body += f"　… 另有 {len(new_items)-6} 条\n"

                # 资料备忘：直接列在弹窗里，不用切窗口翻
                prof = load_profile()
                clip_text = ""
                memo = []
                if prof.get("permit_last6"):
                    clip_text = prof["permit_last6"]
                    memo.append(f"通行证后6位：{clip_text}")
                if prof.get("evisa_ref"):
                    memo.append(f"e-Visa编号：{prof['evisa_ref']}")
                if prof.get("birth_d") and prof.get("birth_y"):
                    memo.append(f"出生日/年：{prof['birth_d']} / "
                                f"{prof['birth_y']}")
                if prof.get("query_code"):
                    memo.append(f"查询编号：{prof['query_code']}")
                if memo:
                    body += "\n" + "\n".join(memo) + "\n"

                hint = ("点「确定」→ 复制后6位并打开预约页"
                        if clip_text else "点「确定」→ 打开预约页")
                if not n.get("open_browser"):
                    hint = ("点「确定」→ 只复制后6位（预约页开关已关）"
                            if clip_text else "点「确定」→ 关闭窗口")
                body += f"\n{hint}\n（点「取消」则不跳转）"

                def _go(_clip=clip_text, _open=bool(n.get("open_browser"))):
                    """用户点确定后：先复制号码，再（按开关）打开预约页。"""
                    if _clip:
                        okc = notifier.set_clipboard(_clip)
                        log(f"已复制通行证后6位到剪贴板 "
                            f"({'成功' if okc else '失败'})", "ALERT")
                    if not _open:
                        log("「点确定后打开预约页」已关闭，不跳转", "INFO")
                        return
                    try:
                        webbrowser.open(W.BOOKING_URL, new=2)
                        log(f"已打开预约页: {W.BOOKING_URL}", "ALERT")
                    except Exception as e:
                        log(f"打开预约页失败: {e}", "ERROR")

                ok, how = notifier.notify(
                    title, body,
                    sound=n.get("sound", True),
                    on_confirm=_go)
                if ok:
                    log(f"提醒已发出 -> {how}", "ALERT")
                    with _state_lock:
                        STATE["notification_test"] = f"成功: {how}"
                elif "跳过" in how:
                    log(f"本次不弹窗 -> {how}", "INFO")
                else:
                    log(f"★ 提醒失败: {how}", "ERROR")
                    with _state_lock:
                        STATE["notification_test"] = "失败: " + str(how)[:200]
            except Exception as e:
                log(f"弹窗异常: {type(e).__name__}: {e}", "ERROR")
                with _state_lock:
                    STATE["notification_test"] = "异常: " + str(e)[:200]
        elif n.get("sound"):
            try:
                ok = notifier.beep(3)
                log(f"响铃: {'成功' if ok else '失败'}", "INFO")
            except Exception as e:
                log(f"响铃异常: {e}", "WARN")
        # open_browser 已并入弹窗的「点确定」动作，这里不再自动开，
        # 否则会和弹窗里点确定打开的那次重复开两个标签页
    else:
        log("处于免打扰时段，本次不打扰", "INFO")

    return new_items


def norm_focus(cfg):
    """把配置的 focus_dates 规整成 {date对象: 原始字符串}。
    兼容用户手输的 MM/DD、MM-DD、YYYY-MM-DD 等各种写法。"""
    out = {}
    for s in (cfg.get("focus_dates") or []):
        if not isinstance(s, str):
            continue
        s = s.strip()
        if not s:
            continue
        dt = None
        # 覆盖用户/前端可能给的写法：
        #   10/01/2026  10/01/26  2026/10/01  2026-10-01  10/01  10-01
        for f in ("%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%m-%d-%Y",
                  "%m-%d-%y", "%Y-%m-%d", "%m/%d", "%m-%d"):
            try:
                dt = datetime.strptime(s, f)
                break
            except ValueError:
                continue
        if dt is None:
            continue
        if dt.year == 1900:              # 只给了月日 -> 补年份
            today = datetime.now().date()
            for y in (today.year, today.year + 1):
                try:
                    cand = datetime(y, dt.month, dt.day).date()
                except ValueError:
                    continue
                if cand >= today - timedelta(days=7):
                    dt = datetime(y, dt.month, dt.day)
                    break
        try:
            d = dt.date()
        except ValueError:
            continue
        out[d] = f"{d.month:02d}/{d.day:02d}/{d.year}"
    return out


def focus_boost(cfg):
    """指定日期临近时返回更短的轮询间隔（秒）。不临近返回 None。

    距指定日 <=1 天 -> 3s(最紧)  <=3 天 -> 5s  <=7 天 -> 8s  <=14 天 -> 10s
    这是「提前蓄力」：放号在上午，真正要动手的是指定日当天，
    所以越临近越要盯得紧。
    """
    fs = norm_focus(cfg)
    if not fs:
        return None
    today = datetime.now().date()
    gaps = [(d - today).days for d in fs if d >= today]
    if not gaps:
        return None
    g = min(gaps)
    if g <= 1:
        return 3
    if g <= 3:
        return 5
    if g <= 7:
        return 8
    if g <= 14:
        return 10
    return None


def check_maintenance():
    """探测预约系统是否在维护。

    数据源只能是**旧版网关 579**：它会 307 跳到 sm/maintenance_579.html，
    页面里写着恢复时间。新版入口（gov.hk 壳页 -> entry 57A）被 Incapsula
    WAF 挡着，脚本读不到内容，只能靠 579 这条线索反推。

    返回 dict：{"on": bool, "resume": "YYYY-MM-DD HH:MM" 或 None,
               "note": 原文摘要, "checked_at": ..., "source": ...}
    """
    global _maint_cache
    now = time.time()
    with _state_lock:
        cache = _maint_cache
        if cache and now - cache.get("_ts", 0) < 600:
            return {k: v for k, v in cache.items() if not k.startswith("_")}

    import urllib.request
    import urllib.error
    import re as _re

    info = {"on": False, "resume": None, "note": "", "source": "579",
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        req = urllib.request.Request(W.BOOKING_URL_LEGACY, headers={
            "User-Agent": W.UA,
            "Accept-Language": "zh-HK,zh;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            final = resp.url
            body = resp.read().decode("utf-8", "replace")
        if "maintenance" in final or "维护" in body or "維護" in body:
            info["on"] = True
            m = _re.search(
                r"预计于\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
                r"[^0-9]*?(上午|下午)?\s*(\d{1,2})\s*时\s*(\d{1,2})?\s*分?",
                body)
            if not m:
                m = _re.search(
                    r"預計於\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
                    r"[^0-9]*?(上午|下午)?\s*(\d{1,2})\s*時\s*(\d{1,2})?\s*分?",
                    body)
            if m:
                y, mo, d, ap, hh, mm = m.groups()
                h = int(hh)
                if ap == "下午" and h < 12:
                    h += 12
                info["resume"] = f"{y}-{int(mo):02d}-{int(d):02d} " \
                                 f"{h:02d}:{int(mm or 0):02d}"
            txt = _re.sub(r"<[^>]+>", " ", body)
            txt = _re.sub(r"\s+", " ", txt).replace("Close 關閉", "").strip()
            info["note"] = txt[:200]
            log(f"⚠ 预约系统维护中，预计 {info['resume']} 恢复", "WARN")
    except Exception as e:
        info["note"] = f"探测失败: {type(e).__name__}"
    with _state_lock:
        _maint_cache = dict(info, _ts=now)
    return info


def in_quiet_hours(cfg):
    q = cfg.get("quiet_hours", {})
    if not q.get("enabled"):
        return False
    try:
        hh, mm = map(int, q["from"].split(":"))
        h2, m2 = map(int, q["to"].split(":"))
    except Exception:
        return False
    now = datetime.now()
    cur = now.hour * 60 + now.minute
    a, b = hh * 60 + mm, h2 * 60 + m2
    if a <= b:
        return a <= cur < b
    return cur >= a or cur < b      # 跨午夜


def backoff(cfg, bi):
    ladder = [cfg["interval"], 15, 25, 40, 60, 90, 120, 180]
    return ladder[min(bi, len(ladder) - 1)]


def parse_bool(v, default=True):
    """宽松 bool 解析：字符串 "false"/"0"/"off"/"" 都算 False。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("false", "0", "off", "no", ""):
            return False
        if s in ("true", "1", "on", "yes"):
            return True
        return default
    return default


def pick_wait(cfg, bi, focus_sec):
    """决定这一轮等多久再查。

    优先级：先按用户设定/退避算出基础间隔，再让「指定日提速」把间隔
    **往快里压**——但绝不往慢里拉。

    为什么不能直接 `return focus_sec`：
      focus_boost 是按「距指定日天数」查表（<=14 天 -> 10 秒）。
      如果用户手动设了 3 秒又指定了一个 14 天后的日期，
      直接返回 focus_sec(10) 会把用户的 3 秒**拖慢**到 10 秒 —— 提速逻辑
      反而变成减速，界面上就会出现「设了 3 秒却显示间隔 10s」的矛盾。
      正确语义是 `min(基础间隔, 提速值)`：提速只允许更快。
    """
    if cfg.get("fixed_interval", True):
        base = cfg["interval"]
    else:
        base = cfg["interval"] if bi == 0 else backoff(cfg, bi)
    if focus_sec:
        return min(base, focus_sec)
    return base


def monitor_loop(cfg_getter, stop_event):
    """监控主循环。配置热更新：每轮重新读取。任何异常都不许中断循环。"""
    bi = 0
    log("监控线程已启动")
    boosted = None
    while not stop_event.is_set():
        try:
            cfg = cfg_getter()
            new = poll_once(cfg)
            if new:
                bi = 0                       # 有变化 -> 回到高频
            else:
                bi = min(bi + 1, 7)
            # 指定日期临近 -> 把间隔往快里压（不会往慢里拉，见 pick_wait）
            fb = focus_boost(cfg)
            if fb and fb < cfg["interval"] and fb != boosted:
                boosted = fb
                log(f"指定日临近，轮询提速至 {fb}s", "ALERT")
            elif not fb:
                boosted = None
            wait = pick_wait(cfg, bi, fb)
        except Exception as e:
            log(f"轮询异常(已忽略, 继续): {type(e).__name__}: {e}", "ERROR")
            bi = min(bi + 1, 7)
            try:
                wait = cfg["interval"]
            except Exception:
                wait = 30
        with _state_lock:
            STATE["next_check_in"] = wait
            STATE["backoff_idx"] = bi
        stop_event.wait(wait)
    log("监控线程已退出")


# ---------------------------------------------------------------- HTTP

HTML_PATH = os.path.join(HERE, "console.html")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass        # 静默，避免刷屏

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, path=None):
        try:
            with open(path or HTML_PATH, encoding="utf-8") as f:
                body = f.read().encode("utf-8")
        except OSError:
            body = b"<h1>page not found</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            return self._html()
        if p in ("/profile", "/profile.html"):
            return self._html(os.path.join(HERE, "profile.html"))
        if p == "/api/profile":
            return self._json(load_profile())
        if p == "/api/state":
            cfg = load_config()
            with _state_lock:
                s = dict(STATE)
            s["config"] = cfg
            s["offices"] = W.OFFICE_NAMES
            s["booking_url"] = W.BOOKING_URL
            s["booking_url_tc"] = W.BOOKING_URL_TC
            s["booking_direct"] = W.BOOKING_URL_DIRECT
            s["booking_legacy"] = W.BOOKING_URL_LEGACY
            s["quota_url"] = W.QUOTA_URL.format(ts=0)
            # 指定日临近信息（前端直接用，别自己算年份）
            fcfg = {"focus_dates": cfg.get("focus_dates") or []}
            nf = norm_focus(fcfg)
            today = datetime.now().date()
            gaps = [(d - today).days for d in nf if d >= today]
            s["focus_left"] = min(gaps) if gaps else None
            s["focus_boost"] = focus_boost(fcfg)
            # 当前「真实生效」的间隔：按当前配置即时算，不等下一轮
            # （next_check_in 是上一轮留下的值，配置刚改时会滞后，
            #   前端拿它显示就会出现「设了 3 秒却显示 10s」）
            with _state_lock:
                bi_now = STATE.get("backoff_idx", 0)
            fb_now = s["focus_boost"]
            s["effective_interval"] = pick_wait(cfg, bi_now, fb_now)
            s["focus_active"] = bool(fb_now and fb_now < cfg["interval"])
            s["booking_fallback"] = W.BOOKING_FALLBACK
            s["maintenance"] = check_maintenance()
            return self._json(s)
        if p == "/api/config":
            return self._json(load_config())
        if p == "/api/history":
            return self._json(load_history())
        return self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        try:
            p = urlparse(self.path).path
            if p == "/api/profile":
                return self._json({"ok": delete_profile()})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            log(f"DELETE 异常: {type(e).__name__}: {e}", "ERROR")
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            self._post()
        except (ConnectionResetError, BrokenPipeError):
            pass          # 客户端提前断开，不该影响服务
        except Exception as e:
            log(f"POST 处理异常: {type(e).__name__}: {e}", "ERROR")
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def _post(self):
        p = urlparse(self.path).path
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._json({"error": "bad json"}, 400)
        if not isinstance(data, dict):
            return self._json({"error": "expect object"}, 400)

        if p == "/api/config":
            cfg = load_config()
            old_focus = list(cfg.get("focus_dates") or [])
            # 合并更新
            for k in ("offices", "within_days", "min_state", "interval",
                      "auto_start", "focus_dates", "fixed_interval",
                      "service_hours", "alert_gap"):
                if k in data:
                    cfg[k] = data[k]
            if "notify" in data and isinstance(data["notify"], dict):
                cfg["notify"].update(data["notify"])
            if "quiet_hours" in data and isinstance(data["quiet_hours"], dict):
                cfg["quiet_hours"].update(data["quiet_hours"])
            # 校验：办事处代号必须合法，否则后面按名取会 KeyError
            if isinstance(cfg.get("offices"), str):
                cfg["offices"] = [cfg["offices"]]
            valid = [o for o in (cfg.get("offices") or [])
                     if o in W.OFFICE_NAMES]
            if not valid:
                return self._json(
                    {"error": "至少选一个办事处。可选: " +
                              ", ".join(f"{k}({v})" for k, v in
                                        W.OFFICE_NAMES.items())}, 400)
            cfg["offices"] = valid
            try:
                cfg["within_days"] = max(0, int(cfg.get("within_days", 0)))
            except (TypeError, ValueError):
                cfg["within_days"] = 0
            try:
                cfg["interval"] = max(3, int(cfg.get("interval", 10)))
            except (TypeError, ValueError):
                cfg["interval"] = 10
            try:
                cfg["min_state"] = int(cfg.get("min_state", 2))
            except (TypeError, ValueError):
                cfg["min_state"] = 2
            if cfg["min_state"] not in (2, 3):
                cfg["min_state"] = 2
            # fixed_interval 必须是真 bool：字符串 "false"/"0" 都是非空串，
            # 直接 bool() 会得到 True（Python 陷阱）
            cfg["fixed_interval"] = parse_bool(cfg.get("fixed_interval"), True)
            if cfg.get("service_hours") not in ("any", "regular", "extended"):
                cfg["service_hours"] = "any"
            try:
                cfg["alert_gap"] = max(0, min(3600,
                                              int(cfg.get("alert_gap", 90))))
            except (TypeError, ValueError):
                cfg["alert_gap"] = 90
            # 指定日期：规整格式 + 限量，解析不了的就丢掉
            fd = cfg.get("focus_dates")
            if isinstance(fd, str):
                fd = [x for x in re.split(r"[,\s;，、]+", fd) if x]
            if not isinstance(fd, list):
                fd = []
            nf = norm_focus({"focus_dates": fd})
            cfg["focus_dates"] = sorted(nf.values(), key=lambda s: s[6:])
            if len(cfg["focus_dates"]) > 20:
                cfg["focus_dates"] = cfg["focus_dates"][:20]
            save_config(cfg)
            with _state_lock:
                STATE["fingerprint"] = None      # 配置变了，重新建基线
                # 指定日期列表变了 -> 清掉「已报过」记录，让新加的日期
                # 即使本来就有号也能立刻提醒（否则改完没反应）
                old_fd = set(old_focus or [])
                if old_fd != set(cfg["focus_dates"]):
                    STATE["focus_reported"] = {}
                    STATE["focus_last"] = {}
            return self._json({"ok": True, "config": cfg})

        if p == "/api/refresh":
            cfg = load_config()
            new = poll_once(cfg, quiet=True)
            with _state_lock:
                s = dict(STATE)
            return self._json({"ok": True, "new": new, "state": s})

        if p == "/api/test-notify":
            cfg = load_config()
            n = cfg["notify"]
            ok = True
            how = ""
            try:
                if n.get("desktop"):
                    prof = load_profile()
                    clip = prof.get("permit_last6") or ""
                    do_open = bool(n.get("open_browser"))

                    def _test_go(_c=clip, _open=do_open):
                        """与真实提醒完全一致的「点确定后」行为，便于验证。"""
                        if _c:
                            okc = notifier.set_clipboard(_c)
                            log(f"[测试] 已复制通行证后6位到剪贴板 "
                                f"({'成功' if okc else '失败'})", "ALERT")
                        if not _open:
                            log("[测试] 「点确定后打开预约页」已关闭，不跳转",
                                "INFO")
                            return
                        try:
                            webbrowser.open(W.BOOKING_URL, new=2)
                            log(f"[测试] 已打开预约页: {W.BOOKING_URL}",
                                "ALERT")
                        except Exception as e:
                            log(f"[测试] 打开预约页失败: {e}", "ERROR")

                    extra_memo = []
                    if clip:
                        extra_memo.append(f"通行证后6位：{clip}")
                    if prof.get("evisa_ref"):
                        extra_memo.append(f"e-Visa编号：{prof['evisa_ref']}")
                    if prof.get("birth_d") and prof.get("birth_y"):
                        extra_memo.append(f"出生日/年：{prof['birth_d']} / "
                                          f"{prof['birth_y']}")
                    if prof.get("query_code"):
                        extra_memo.append(f"查询编号：{prof['query_code']}")

                    tb = "这是一条测试提醒\n\n看到窗口 + 听到提示音 = 提醒链路正常。"
                    if extra_memo:
                        tb += "\n\n" + "\n".join(extra_memo)
                    tb += "\n\n点「确定」会复制号码并打开预约页（如果资料里有）。"
                    ok, how = notifier.notify(
                        "香港身份证预约监控 · 测试", tb,
                        sound=n.get("sound", True), force=True,
                        on_confirm=_test_go)
                elif n.get("sound"):
                    ok = notifier.beep(3)
                    how = "beep"
            except Exception as e:
                ok = False
                how = f"异常 {type(e).__name__}: {e}"
            msg = (f"成功: {how}" if ok else f"失败: {how}")
            log(f"测试提醒 -> {msg}", "INFO" if ok else "ERROR")
            with _state_lock:
                STATE["notification_test"] = msg
            return self._json({"ok": ok, "how": how})

        if p == "/api/reset":
            with _state_lock:
                STATE["fingerprint"] = None
                STATE["recent"] = []
            return self._json({"ok": True})

        if p == "/api/profile":
            return self._json(save_profile(data))

        if p == "/api/open-booking":
            # 用服务端 webbrowser 打开系统默认浏览器，比页面 window.open
            # 可靠（预览面板/内嵌浏览器常拦截弹窗）
            url = data.get("url") or W.BOOKING_URL
            if not isinstance(url, str) or not url.startswith(
                    ("https://www.gov.hk",
                     "https://entry.es2.immd.gov.hk",
                     "https://eservices.es2.immd.gov.hk",
                     "https://webapp.es2.immd.gov.hk")):
                url = W.BOOKING_URL          # 白名单，防止被当跳板
            try:
                webbrowser.open(url, new=2)
                ok = True
            except Exception as e:
                ok = False
                log(f"打开浏览器失败: {type(e).__name__}: {e}", "ERROR")
            intent = data.get("intent") if isinstance(data.get("intent"),
                                                       str) else "new"
            if intent not in ("new", "change", "enquire", "reminder"):
                intent = "new"
            label = W.BOOKING_PAGES.get(intent, ("预约", ""))[0]
            log(f"[手动] 打开官方入口（意图={label}）: {url}")
            return self._json({"ok": ok, "url": url, "intent": intent,
                               "maintenance": check_maintenance()})

        return self._json({"error": "not found"}, 404)


def main():
    global STOP
    cfg = load_config()
    save_config(cfg)      # 首次自动落盘，方便用户直接改

    with _state_lock:
        STATE["running"] = True
        STATE["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    t = threading.Thread(target=monitor_loop, args=(load_config, STOP),
                         daemon=True)
    t.start()

    url = f"http://127.0.0.1:{PORT}"
    log("=" * 60)
    log(f"可视化控制台已启动: {url}")
    log(f"配置文件: {CONFIG_PATH}")
    log("关闭: Ctrl+C")
    log("=" * 60)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("收到 Ctrl+C, 正在退出…")
    finally:
        STOP.set()
        with _state_lock:
            STATE["running"] = False
        time.sleep(0.3)
        srv.shutdown()


if __name__ == "__main__":
    main()
