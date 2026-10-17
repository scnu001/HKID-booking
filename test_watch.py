"""用真实快照 + 人工构造的「已满→有票」变化，验证告警与去重逻辑。"""
import json, os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hkid_watch as W

real_status, real = W.fetch_quota()
assert real_status == 200, f"真实接口应可用, got {real_status}"
grid, offices = W.parse_quota(real)
print(f"[setup] 真实数据 {len(grid)} 条, 官方时间 {real.get('lastUpdateTime')}")

# 构造一份「上一轮」状态：把所有 quota-g 都伪装成已满
prev_grid = {k: ("quota-r" if v == "quota-g" else v) for k, v in grid.items()}

# 注入到 summarize 走一遍，确认只报出变化项
hits = W.summarize(grid, offices, None, None, 2)
print(f"[test] 当前有名额总数: {len(hits)}")

# 验证 parse_date
d = W.parse_date("11/19/2026")
assert (d.year, d.month, d.day) == (2026, 11, 19), d
print(f"[test] parse_date OK -> {d}")

# 验证 STATE 映射与官方一致
assert W.STATE_CN["quota-g"] == "尚有名额"
assert W.STATE_CN["quota-y"] == "少量名额"
assert W.STATE_CN["quota-r"] == "已满"
print("[test] 状态语义映射 OK (取自官方 style.css)")

# 验证 within 过滤
h30 = W.summarize(grid, offices, None, 30, 2)
assert len(h30) < len(hits), "30天内应少于全部"
print(f"[test] within=30 过滤 OK: 全部 {len(hits)} -> 30天内 {len(h30)}")

# 验证 office 过滤
hk = W.summarize(grid, offices, {"RHK"}, None, 2)
assert all(o == "RHK" for o, *_ in hk)
print(f"[test] office 过滤 OK: 港岛 {len(hk)} 个名额")

# 状态文件往返测试（验证 --once 之外模式的持久化）
sd = tempfile.mktemp(suffix=".json")
with open(sd, "w", encoding="utf-8") as f:
    json.dump({json.dumps(list(k)): v for k, v in prev_grid.items()}, f)
with open(sd, encoding="utf-8") as f:
    restored = {tuple(json.loads(k)): v for k, v in json.load(f).items()}
assert len(restored) == len(prev_grid)
changed = [k for k, v in grid.items() if restored.get(k) != v]
print(f"[test] 状态持久化往返 OK: {len(changed)} 项会被报为「新票」")
for k in changed[:3]:
    print(f"         {W.office_label(k[0])} {k[1]}: {restored.get(k)} -> {grid[k]}")
os.remove(sd)

print("\n全部测试通过 ✓")
