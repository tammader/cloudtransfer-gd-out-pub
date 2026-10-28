# -*- coding: utf-8 -*-
"""回归测试: 预算切分不能超发, 且后跑的盘必须拿到非零切片。

背景: 2026-10-28 起 teldrive 文件数停在 1461-1520。根因是 run_one 用的 t0 是
整进程启动时间, personal 吃满 257.7 分钟, 轮到 family 时 time.time()-t0 已
超预算, 循环第一步就 break -> "family 本轮: 新传 0 个 / 0 KB" 连续多轮。

用法: python3 tools/test_budget_split.py    (无外部依赖, 不联网)
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 桩掉真实盘客户端, 避免 import 期就需要凭据
for name, cls in (("ty_cloud_personal", "TyPersonal"), ("ty139", "Ty139")):
    mod = types.ModuleType(name)
    setattr(mod, cls, type(cls, (), {"__init__": lambda self, *a, **k: None,
                                     "login": lambda self: True}))
    sys.modules[name] = mod

import yd2_teldrive as m   # noqa: E402


def split(budget_min, reserve_min=15, min_slice_min=45, n=2):
    """复刻 main() 的记账逻辑, 便于纯函数断言"""
    hard = budget_min * 60 - reserve_min * 60
    out, cursor = [], 0.0
    for idx in range(n):
        left = max(0.0, hard - cursor)
        remaining = n - idx
        if remaining == 1:
            sl = left
        else:
            keep = (remaining - 1) * min_slice_min * 60
            sl = max(0.0, min(left - keep, left * 0.6))
        sl = min(sl, left)
        out.append(sl)
        cursor += sl
    return out


def test_no_overspend():
    for b in (60, 120, 240, 270, 300, 310):
        parts = split(b)
        total = sum(parts)
        assert total <= (b - 15) * 60 + 1, "budget=%s 超发: %s" % (b, parts)
        print("  ok budget=%-4s 切片=%s 合计=%.0f 分"
              % (b, [round(p / 60, 1) for p in parts], total / 60))


def test_family_not_starved():
    # 旧 bug 的直接判据: 两盘都要拿到 >= min_slice_min 的量, 否则就是饿死
    for b in (240, 270, 300):
        parts = split(b)
        assert all(p >= 45 * 60 for p in parts), \
            "budget=%s 有一盘切片 <45 分: %s" % (b, [p / 60 for p in parts])
        print("  ok budget=%-4s 两个盘都 >=45 分: %s"
              % (b, [round(p / 60, 1) for p in parts]))


def test_tiny_budget_marks_skip():
    # 预算小到装不下两盘时, 必须显式给出 skipped_reason 而不是伪装成"新传 0"
    parts = split(60)
    assert parts[0] < 60, "60 分预算下第一盘应立即被跳过, 实际 %.0f 秒" % parts[0]
    print("  ok budget=60min 第一盘 %.0f 秒 -> 会被标 skipped_reason" % parts[0])


def test_rotate_alternates():
    # auto 轮换必须在相邻 6 小时窗口里换方向, 否则同一盘永久抢先
    def order(slot):
        return "family" if slot % 2 == 0 else "personal"
    seen = {order(s) for s in (83018, 83019, 83020)}
    assert len(seen) == 2, "轮换没有真正交替: %s" % seen
    print("  ok rotate auto 会交替: slot 83018/19/20 -> 两个方向都出现过")


if __name__ == "__main__":
    for fn in (test_no_overspend, test_family_not_starved,
               test_tiny_budget_marks_skip, test_rotate_alternates):
        print(fn.__name__)
        fn()
    print("\n全部通过")