#!/usr/bin/env python3
"""把 command 服務的 OpenAPI 規格匯出成檔案。

**為什麼要匯出成靜態檔**：`/openapi.json` 只有服務跑著時才拿得到，
而要跟外部整合的人談介面時，服務通常不在他手上。匯出的檔案進 git，
**介面的變更因此看得到 diff**——那是口頭約定做不到的事。

用法（服務要在跑）：
    python3 scripts/export-openapi.py            # 寫到 doc/openapi.json
    python3 scripts/export-openapi.py --check    # 只檢查有沒有漂移（CI 用）
"""
import argparse
import json
import pathlib
import sys
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://localhost:38001/openapi.json")
ap.add_argument("--out", default="doc/openapi.json")
ap.add_argument("--check", action="store_true",
                help="不寫檔，只比對現有檔案是否已過期")
a = ap.parse_args()

try:
    with urllib.request.urlopen(a.url, timeout=10) as r:
        spec = json.loads(r.read().decode())
except Exception as e:
    print(f"**取不到 {a.url}**（{e}）——command 服務要在跑")
    sys.exit(2)

# 排序鍵值：同一份規格每次匯出要位元組相同，否則 diff 會充滿雜訊而沒人看
text = json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
out = pathlib.Path(a.out)

if a.check:
    old = out.read_text(encoding="utf-8") if out.exists() else ""
    if old == text:
        print(f"✓ {a.out} 是最新的")
        sys.exit(0)
    print(f"✗ **{a.out} 已過期**——端點改了但規格沒重匯。"
          f"跑 `python3 scripts/export-openapi.py` 更新並 commit")
    sys.exit(1)

out.write_text(text, encoding="utf-8")
paths = spec.get("paths", {})
tagged = {}
for p, ops in paths.items():
    for m, op in ops.items():
        for t in op.get("tags") or ["(未分組)"]:
            tagged.setdefault(t, []).append(f"{m.upper()} {p}")
print(f"已寫入 {a.out}（{len(paths)} 條路徑）")
for t in sorted(tagged):
    print(f"  [{t}] {len(tagged[t])} 個")
    if t == "任務":
        for line in sorted(tagged[t]):
            print(f"      {line}")
