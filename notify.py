#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
桌面提醒模块（Windows，零第三方依赖）。

## 为什么重写

之前两版都用 toast / 气泡，用户反馈「完全没反应」。真实原因有三层：

1. **toast 用了未注册的 AUMID**
   `CreateToastNotifier('HKID 配额监控')` 里那个 AppID 系统里根本没有。
   Win10/11 对未注册 AUMID 的 toast 会**静默丢弃** —— `Show()` 正常返回，
   `TOAST_OK` 也打出来了，但屏幕上什么都不出现。**「没报错」≠「显示了」**。
   注册过的只有 `{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe` 等。

2. **`ShowBalloonTip` 在 Win10+ 走同一套 toast 管道**，一样被吞。

3. **`winsound.Beep` 在很多机器上走的是 PC 蜂鸣器通道**，
   现代笔记本/台式机常常没有、或者被静音策略忽略 → 听不见。
   要走默认音频设备必须用 `PlaySound(..., SND_ALIAS)` 播系统声音。

## 现在的方案

主路线 = **`MessageBoxW` 纯 Win32 弹窗**
  · 真窗口，不是「通知」，不受通知中心 / AppID / 专注助手影响
  · `MB_TOPMOST | MB_SETFOREGROUND` 强制置顶抢焦点
  · 用 `MessageBoxTimeoutW` 自动关闭，不会卡死监控主循环
  · 在**独立线程**里弹，`poll_once` 该继续跑继续跑

声音 = **`winsound.PlaySound` 系统别名**（`SystemExclamation` 等）
  · 走默认音频设备，跟随系统音量
  · `SND_ASYNC` 不阻塞
  · 再用 `Beep` 兜底（有 PC 蜂鸣器的机器能听到）

toast / 气泡降级为**附加**手段，不再作为主要依赖。
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import threading
import time

try:
    import winsound
except ImportError:                       # 非 Windows
    winsound = None

_PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

_user32 = None
if os.name == "nt":
    try:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
    except Exception:
        _user32 = None

# ---- MessageBox 常量
# 用 MB_OKCANCEL 而不是 MB_OK：
#   MB_OK      超时返回 1（和「点确定」一模一样，无法区分！）
#   MB_OKCANCEL 超时返回 32000、点确定 1、点取消 2 —— 能准确分辨
# 这个差异是实测出来的：MB_OK 会让人误以为「没点确定也跳转了」。
MB_OK = 0x00000000
MB_OKCANCEL = 0x00000001
MB_ICONWARNING = 0x00000030
MB_ICONINFORMATION = 0x00000040
MB_TOPMOST = 0x00040000
MB_SETFOREGROUND = 0x00010000
MB_SYSTEMMODAL = 0x00001000

_lock = threading.Lock()
_last_box = {"at": 0.0}
# 两次弹窗的最小间隔（秒）。放号后数据会有多轮抖动，太短会连续弹很多次。
_min_gap = 90.0
# 当前是否有弹窗正开着。有的话新的直接跳过 —— 否则用户会看到两个窗口叠在
# 一起（MessageBox 是模态的，前一个没关时再弹会再开一个）。
_popup_open = False


# ---------------------------------------------------------------- 声音
def play_sound(strong=True):
    """播系统提示音。返回 (ok, 说明)。"""
    if winsound is None:
        return False, "no winsound"
    errs = []
    # 1) 系统声音别名：走默认音频设备，跟随音量
    aliases = (["SystemExclamation", "SystemHand"]
               if strong else ["SystemAsterisk"])
    played = 0
    for a in aliases:
        try:
            winsound.PlaySound(a, winsound.SND_ALIAS | winsound.SND_ASYNC)
            played += 1
            time.sleep(0.25)
        except Exception as e:
            errs.append(f"{a}: {type(e).__name__}")
    # 2) Beep 兜底（有 PC 蜂鸣器的机器）
    try:
        for i in range(3 if strong else 2):
            winsound.Beep(1400 if i % 2 == 0 else 1800, 220)
    except Exception as e:
        errs.append(f"beep: {type(e).__name__}")
    if played:
        return True, f"alias x{played}"
    return False, " | ".join(errs) or "silent"


# ---------------------------------------------------------------- 弹窗
def set_clipboard(text):
    """把文本放进剪贴板（Win32 直接操作，不依赖 PowerShell）。

    有票时把「通行证后 6 位」自动放进剪贴板，用户切到预约页直接 Ctrl+V。

    ⚠️ 64 位 Python 的坑：ctypes 默认把函数返回值当 c_int(32位)，
    而 GlobalAlloc/GlobalLock 返回的是**指针**，会被截断成无效值，
    导致 SetClipboardData 失败但没有任何报错。必须显式声明 restype。
    """
    if _user32 is None or not text:
        return False
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        u = _user32
    except Exception:
        return False

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k.GlobalFree.argtypes = [ctypes.c_void_p]
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    u.EmptyClipboard.argtypes = []
    u.SetClipboardData.restype = ctypes.c_void_p
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u.CloseClipboard.argtypes = []

    # 剪贴板可能被别的程序短暂占用，重试几次
    opened = False
    for _ in range(6):
        if u.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.08)
    if not opened:
        return False
    try:
        u.EmptyClipboard()
        data = str(text)
        size = (len(data) + 1) * ctypes.sizeof(ctypes.c_wchar)
        h = k.GlobalAlloc(GMEM_MOVEABLE, size)
        if not h:
            return False
        p = k.GlobalLock(h)
        if not p:
            k.GlobalFree(h)
            return False
        ctypes.memmove(p, ctypes.create_unicode_buffer(data), size)
        k.GlobalUnlock(h)
        # SetClipboardData 成功后所有权归系统，不能再 GlobalFree
        if not u.SetClipboardData(CF_UNICODETEXT, h):
            k.GlobalFree(h)
            return False
        return True
    except Exception:
        return False
    finally:
        u.CloseClipboard()


def get_clipboard():
    """读回剪贴板文本（自检用）。"""
    if _user32 is None:
        return None
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalLock.argtypes = [ctypes.c_void_p]
        k.GlobalUnlock.argtypes = [ctypes.c_void_p]
        _user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        _user32.GetClipboardData.restype = ctypes.c_void_p
        _user32.GetClipboardData.argtypes = [ctypes.c_uint]
        if not _user32.OpenClipboard(None):
            return None
        try:
            h = _user32.GetClipboardData(13)
            if not h:
                return None
            p = k.GlobalLock(h)
            if not p:
                return None
            val = ctypes.c_wchar_p(p).value
            k.GlobalUnlock(h)
            return val
        finally:
            _user32.CloseClipboard()
    except Exception:
        return None


def _message_box_timeout(title, message, timeout_ms=20000, topmost=True,
                         on_confirm=None):
    """调 MessageBoxTimeoutW（user32 里的隐藏导出，XP 至今都可用）。

    返回 (ok, 说明)。ok=True 表示窗口确实被创建并显示过。
    on_confirm: 用户点「确定」后要执行的回调（超时自动关不触发）。
    """
    if _user32 is None:
        return False, "non-windows"
    flags = MB_OKCANCEL | MB_ICONWARNING | MB_SETFOREGROUND
    if topmost:
        flags |= MB_TOPMOST | MB_SYSTEMMODAL
    try:
        fn = _user32.MessageBoxTimeoutW
    except AttributeError:
        return False, "no MessageBoxTimeoutW"
    fn.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT, wt.WORD, wt.DWORD]
    fn.restype = ctypes.c_int
    t0 = time.time()
    try:
        r = fn(None, message, title, flags, 0, timeout_ms)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    elapsed_ms = (time.time() - t0) * 1000

    # MessageBox 返回值：IDOK=1、IDCANCEL=2、**超时=32000**。
    # 注意：之前误以为超时也返回 1，导致「没点确定就跳转」。
    # 实测纯净环境超时稳定返回 32000（耗时 ≈ timeout+60ms）；
    # 只有在**多个弹窗重叠**时才出现过 1 秒内返回 1 的异常，
    # 因此额外用耗时做一次兜底判断（明显短于超时才认为是真点击）。
    IDOK, IDCANCEL, TIMEOUT = 1, 2, 32000
    looked_like_timeout = elapsed_ms >= timeout_ms - 800
    if r == IDOK and not looked_like_timeout:
        if on_confirm:
            try:
                on_confirm()
            except Exception:
                pass
        return True, f"用户点了确定({elapsed_ms:.0f}ms)"
    if r == IDOK and looked_like_timeout:
        # 返回值是 1 但耗时等于超时 -> 按超时处理，不触发跳转
        return True, f"疑似超时(返回1但耗时{elapsed_ms:.0f}ms)，不跳转"
    if r == IDCANCEL:
        return True, "用户点了取消"
    if r == TIMEOUT:
        return True, f"超时自动关闭({elapsed_ms:.0f}ms)"
    return True, f"返回 {r}({elapsed_ms:.0f}ms)"


def _popup_async(title, message, timeout_ms=20000, verify=True, force=False,
                 on_confirm=None):
    """在独立线程里弹窗，避免阻塞轮询主循环。

    verify=True 时会用 FindWindowW 去**探测窗口是否真的出现在屏幕上**，
    探测到才返回 True。这是「真的可见」而不是「线程启动了」。

    冷却判断统一放在 notify() 里（那里才能连声音一起挡），
    这里只负责起线程。
    """
    with _lock:
        _last_box["at"] = time.time()
    t = threading.Thread(
        target=_safe_box, args=(title, message, timeout_ms, on_confirm),
        daemon=True)
    t.start()
    if not verify or _user32 is None:
        return True, "已弹出"
    # 轮询找窗口句柄，最多等 2.5 秒
    for _ in range(25):
        time.sleep(0.1)
        try:
            hwnd = _user32.FindWindowW(None, title)
        except Exception:
            hwnd = 0
        if hwnd:
            return True, "窗口已显示(已验证)"
    return False, "窗口未出现(可能被系统拦截)"


def _safe_box(title, message, timeout_ms, on_confirm=None):
    global _popup_open
    try:
        _message_box_timeout(title, message, timeout_ms,
                             on_confirm=on_confirm)
    except Exception as e:
        print(f"[notify] 弹窗线程异常: {e}", file=sys.stderr)
    finally:
        with _lock:
            _popup_open = False


# ---------------------------------------------------------------- 附加 toast
def _toast(title, message):
    """尽力而为的原生 toast。用已注册的 PowerShell AUMID，否则会被静默丢弃。"""
    if not os.path.exists(_PS):
        return False, "no powershell"
    import base64
    t = base64.b64encode(title.encode()).decode()
    m = base64.b64encode(message.encode()).decode()
    aumid = (r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
             r"\WindowsPowerShell\v1.0\powershell.exe")
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
        "ContentType=WindowsRuntime]|Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom,"
        "ContentType=WindowsRuntime]|Out-Null;"
        f"$T=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{t}'));"
        f"$M=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{m}'));"
        "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;"
        "$x.LoadXml(\"<toast><visual><binding template='ToastGeneric'>"
        "<text>$T</text><text>$M</text></binding></visual>"
        "<audio src='ms-winsoundevent:Notification.Looping.Alarm2'/></toast>\");"
        "$to=[Windows.UI.Notifications.ToastNotification]::new($x);"
        f"[Windows.UI.Notifications.ToastNotificationManager]::"
        f"CreateToastNotifier('{aumid}').Show($to);"
        "Write-Output 'TOAST_SENT'"
    )
    try:
        r = subprocess.run([_PS, "-NoProfile", "-NonInteractive",
                            "-Command", script],
                           capture_output=True, timeout=25)
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        if "TOAST_SENT" in out:
            return True, "toast sent"
        return False, (err or out or f"rc={r.returncode}")[:200]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- 对外接口
def notify(title, message, sound=True, force=False, on_confirm=None,
           cooldown=None):
    """主入口：先响，再弹，最后补 toast。

    返回 (ok, 方式说明)。ok 的含义是「至少有一条通道真的触发了」，
    不再是「Popen 没报错」那种假回执。
    force=True 用于测试按钮，跳过冷却。
    on_confirm: 用户点「确定」后执行（复制剪贴板 + 打开预约页）。
    cooldown: 本次允许的冷却秒数，None 用全局 _min_gap。

    ⚠️ 冷却必须在**这里**判断，不能只在 _popup_async 里判。
    之前声音在冷却判断之前就播了，表现是「弹窗不重复但一直响」——
    用户听到的仍然是「反复提醒」。
    """
    gap = _min_gap if cooldown is None else float(cooldown)
    global _popup_open
    with _lock:
        # 已有弹窗没关 -> 完全静默（不响、不弹、不 toast）
        if _popup_open:
            return False, "已有弹窗未关闭，跳过"
        if not force:
            passed = time.time() - _last_box["at"]
            if passed < gap:
                return False, (f"冷却中（已过 {passed:.0f}s / {gap:.0f}s），"
                               f"本次静默跳过")
        _popup_open = True
        _last_box["at"] = time.time()

    ways = []
    oks = []

    if sound:
        ok, how = play_sound(True)
        oks.append(ok)
        ways.append("声音:" + how)
        time.sleep(0.35)          # 让响声先出来，再弹窗

    ok, how = _popup_async(title, message, force=True,   # 冷却已在上方判过
                           on_confirm=on_confirm)
    oks.append(ok)
    ways.append("弹窗:" + how)

    # toast 作为额外提醒（弹窗已经可见，这个可有可无）
    ok2, how2 = _toast(title, message)
    ways.append("通知:" + ("ok" if ok2 else str(how2)[:60]))

    return any(oks), " | ".join(ways)


def beep(times=3):
    """只要声音。"""
    ok, _ = play_sound(True)
    return ok


def popup(title, message, timeout_ms=20000):
    """只要弹窗（同步等待结果，测试用）。"""
    return _message_box_timeout(title, message, timeout_ms)


if __name__ == "__main__":
    title = sys.argv[1] if len(sys.argv) > 1 else "HKID 配额提醒"
    msg = sys.argv[2] if len(sys.argv) > 2 else "检测到新名额！"
    if "--sync" in sys.argv:
        print(popup(title, msg, 8000))
    else:
        ok, how = notify(title, msg, sound=True)
        print(f"ok={ok}\n{how}")
        # 弹窗在 daemon 线程里，主进程一退窗口就没了 —— 必须等
        if "--wait" in sys.argv:
            print("等待 10 秒让弹窗可见…")
            time.sleep(10)
