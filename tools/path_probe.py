# -*- coding: utf-8 -*-
"""把几条搬运链的两端打出来(远端名脱敏, 目录名保留) —— 只为核对迁移后的走向

  python3 tools/path_probe.py
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pathcfg                                       # noqa: E402

try:
    import logmask
    logmask.install()
except Exception:
    pass

_td = os.environ.get("PATHS_JSON_TD", "").strip()
if _td:
    try:
        import json
        m = dict(pathcfg._load())
        m.update(json.loads(_td))
        pathcfg._cache = m
    except Exception:
        pass


def h8(s):
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:8]


def shape(v):
    if not v:
        return "(未配置)"
    out = []
    for one in str(v).split(","):
        s = one.strip()
        if not s:
            continue
        if ":" in s and not s.startswith("/"):
            r, p = s.split(":", 1)
            out.append("<r:%s>:%s" % (h8(r), p))
        elif s.startswith("/"):
            seg = s.strip("/").split("/")
            out.append("<m:%s>/%s" % (h8("/" + seg[0]), "/".join(seg[1:])))
        else:
            out.append("<r:%s>" % h8(s))
    return ",".join(out)


CHAINS = [
    ("in2-ydout  (od2:in2 -> ydout, 只挑 _D 后缀, 送达后删源)", ["GR_A_SRC", "GR_A_DST", "GR_A_SUFFIX"]),
    ("ydout-in3  (ydout -> od2:in3, 送达后同盘 move 到归档)", ["GR_B_SRC", "GR_B_DST", "GR_B_ARCH"]),
    ("od2-tg-gd2 (TG 目录 -> TG 目录, 送达后删源)", ["GR_TG_SRC", "GR_TG_DST"]),
    ("出口/落地", ["GD_SRC", "GD_DST", "GD_ARCH", "OT_SRC", "OT_TY", "OT_DBJM", "OT_ARCH",
                "DBJM_SRC", "DBJM_DST", "TY_SRC", "TY_DST", "GB_SRC_ROOT", "GB_DST_ROOT"]),
    ("判重集合/清单", ["ID_OD_DIRS", "ID_STEM_MANIFESTS", "TT_COMPARE_REMOTES", "TT_REMOTE",
                  "TT_OD_MANIFEST", "TT_GD_MANIFEST"]),
    ("报告/状态落点", ["GM_OD", "GB_REPORT", "GD_REPORT", "GR_REPORT", "ID_REPORT",
                  "OT_REPORT", "DBJM_REPORT", "HC_REPORT"]),
]

print("参照: <r:%s> = 旧盘(onedrive2)  <r:%s> = 新盘(gdrive2, gd2)  <r:%s> = 第三个远端"
      % (h8("onedrive2"), h8("gdrive2"), h8("gdrive6")))
print("      gd2 上新建的根目录 = onedrive\n")
for title, keys in CHAINS:
    print("== %s" % title)
    for k in keys:
        v = pathcfg.get(k)
        print("   %-22s %s" % (k, shape(v)))
    print()
