#!/usr/bin/env python3
"""把**對外介面**匯出成一份 OpenAPI 規格。

**為什麼要匯出成靜態檔**：`/openapi.json` 只有服務跑著時才拿得到，而要跟外部
整合的人談介面時，服務通常不在他手上。匯出的檔案進 git，**介面的變更因此看得到
diff**——那是口頭約定做不到的事。

**為什麼要合併兩個服務**：對外介面橫跨 command（`:38001`，指揮）與 backend
（`:38000`，資料與影像），而外部整合的人不該知道我們內部怎麼切服務。合併成
一份，每條路徑自己帶 `servers` 指出它在哪個埠。

**為什麼要過濾**：兩個服務的 `/openapi.json` 裡大部分是**內部端點**——給我們
自己的畫面用的，形狀會隨內部演進而變。把它們一起交出去，外部就會開始依賴
我們沒有承諾過的東西。這裡只放 `doc/external-api-v3.html` 講好的那一組。

**WebSocket 不在 OpenAPI 裡**（規格不支援），所以寫進頂層 description。

用法（兩個服務都要在跑）：
    python3 scripts/export-openapi.py            # 寫到 doc/openapi.json
    python3 scripts/export-openapi.py --check    # 只檢查有沒有漂移（CI 用）
"""
import argparse
import json
import pathlib
import sys
import urllib.request

#: 對外的那一組。**列在這裡才會被交出去**——新增對外端點時要一起改這裡，
#: 不然它不會出現在規格裡，而外部只看規格。
#: key＝路徑前綴或完整路徑；value＝這條在哪個服務。
EXTERNAL = {
    # ── 指揮面（command :38001）────────────────────────────────────
    "/api/ext/drones": "command",          # 入口：有哪些機、指不指得動、影像在哪
    "/api/plans": "command",               # 路徑庫
    "/api/missions": "command",            # 路徑庫（舊名，保留相容）
    "/api/start": "command",               # 一鍵起飛
    "/api/command/{sysid}/mode/{mode}": "command",   # 中斷：rtl／land／hold
    # ── 資料面（backend :38000）───────────────────────────────────
    "/api/ext/missions": "backend",                  # 歷史任務清單（含 state）
    "/api/ext/missions/{mission_id}/live": "backend",    # 即時（輪詢版）
    "/api/ext/missions/{mission_id}/signal": "backend",  # 一趟的完整訊號
    "/api/ext/live": "backend",            # **待移除**，新程式不要用
    # ── 影像（backend :38000）─────────────────────────────────────
    "/api/drones/{drone_id}/camera": "backend",
    "/api/drones/{drone_id}/camera/snapshot.jpg": "backend",
    "/api/drones/{drone_id}/camera/stream.mjpg": "backend",
}

SERVERS = {
    "command": {"url": "http://10.141.2.21:38001", "description": "指揮面"},
    "backend": {"url": "http://10.141.2.21:38000", "description": "資料面與影像"},
}

DESCRIPTION = """\
無人機管理系統的**對外介面**。人看的版本在 `doc/external-api-v3.html`
（含範例、錯誤碼與每個欄位的意思），這份是給機器讀的。

**認證：沒有。**（2026-09-14 定案）任何連得到這兩個埠的人都能看資料、也能
指揮飛機。安全靠網段隔離——這件事寫在這裡，不能靠「大家都知道」。

## WebSocket（OpenAPI 表達不了，寫在這裡）

    ws://10.141.2.21:38000/ws/v1/missions/{mission_id}

起飛後每 0.5 秒推一則。訊息與 `GET /api/v1/ext/missions/{id}/live` 的
`messages` **逐字相同、同一組序號**——兩者可以混用，斷線期間用輪詢頂著，
回來再用 `?after_seq=` 接上。

訊息類型：`hello`／`route`（整條規劃航線，一次給完）／`track`（實際軌跡，
全量）／`state`（每 0.5 秒：位置、電量、`mission_progress`）／
`start_step`（起飛序列跑到哪一步）／`ended`。

## 三件最容易踩到的事

1. **`/api/v1/start` 預設會等整套跑完才回應**（上傳→解鎖→起飛→**飛到任務
   起始點**→切 AUTO）。起點遠就是幾分鐘。**請帶 `wait: false`**：立刻回 202，
   進度走串流。用同步模式而逾時的話，**飛機仍在飛**——不要直接重送。
2. **固定帶自己產生的 `mission_id`。** 帶了，逾時後重送是冪等的（回 202
   `already_running`＋目前步驟）；不帶，重送只會拿到 409 `start_in_progress`。
3. **`ended` 是一次性推送，不是可以事後查的紀錄。** 任務結束後只再保留 30 秒，
   之後重連回 410 `mission_gone`。**資料一點都沒少**——改用
   `GET /api/v1/ext/missions`（看 `state`）與 `.../signal`（拿結果）。

## 路徑前綴

每一支都吃 `/api/v1/…`。舊的無版本寫法保留為別名。
"""


def fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


ap = argparse.ArgumentParser()
ap.add_argument("--command-url", default="http://localhost:38001/openapi.json")
ap.add_argument("--backend-url", default="http://localhost:38000/openapi.json")
ap.add_argument("--out", default="doc/openapi.json")
ap.add_argument("--check", action="store_true",
                help="不寫檔，只比對現有檔案是否已過期")
a = ap.parse_args()

specs = {}
for name, url in (("command", a.command_url), ("backend", a.backend_url)):
    try:
        specs[name] = fetch(url)
    except Exception as e:
        print(f"**取不到 {url}**（{e}）——{name} 服務要在跑")
        sys.exit(2)

merged = {
    "openapi": "3.1.0",
    "info": {"title": "無人機管理系統：對外介面", "version": "v3",
             "description": DESCRIPTION},
    "servers": list(SERVERS.values()),
    "paths": {},
    "components": {"schemas": {}},
}

missing = []
for path, where in EXTERNAL.items():
    item = specs[where].get("paths", {}).get(path)
    if item is None:
        missing.append(f"{path}（{where}）")
        continue
    # 每條路徑自己帶 servers：外部不該知道我們內部怎麼切服務
    merged["paths"][path] = {**item, "servers": [SERVERS[where]]}

# schema 兩邊可能同名而內容不同 → 加前綴，並把 $ref 一起改掉
for where, spec in specs.items():
    for name, schema in (spec.get("components", {}).get("schemas") or {}).items():
        merged["components"]["schemas"][f"{where}.{name}"] = schema


def fix_refs(node, where):
    if isinstance(node, dict):
        return {k: (f"#/components/schemas/{where}."
                    + v.rsplit("/", 1)[1] if k == "$ref" and isinstance(v, str)
                    and v.startswith("#/components/schemas/") else fix_refs(v, where))
                for k, v in node.items()}
    if isinstance(node, list):
        return [fix_refs(x, where) for x in node]
    return node


for path, where in EXTERNAL.items():
    if path in merged["paths"]:
        merged["paths"][path] = fix_refs(merged["paths"][path], where)
merged["components"]["schemas"] = {
    n: fix_refs(s, n.split(".", 1)[0])
    for n, s in merged["components"]["schemas"].items()}

# 排序鍵值：同一份規格每次匯出要位元組相同，否則 diff 會充滿雜訊而沒人看
text = json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
out = pathlib.Path(a.out)

if missing:
    # **不要靜默少一支**：列在 EXTERNAL 卻找不到，多半是端點改名了
    print("⚠ 下面這些列在 EXTERNAL 但服務上找不到（改名了？）：")
    for m in missing:
        print(f"    {m}")

if a.check:
    old = out.read_text(encoding="utf-8") if out.exists() else ""
    if old == text and not missing:
        print(f"✓ {a.out} 是最新的")
        sys.exit(0)
    print(f"✗ **{a.out} 已過期**——端點改了但規格沒重匯。"
          f"跑 `python3 scripts/export-openapi.py` 更新並 commit")
    sys.exit(1)

out.write_text(text, encoding="utf-8")
print(f"已寫入 {a.out}（{len(merged['paths'])} 條對外路徑）")
for path, where in sorted(EXTERNAL.items()):
    mark = "✓" if path in merged["paths"] else "✗"
    ops = " ".join(sorted(m.upper() for m in merged["paths"].get(path, {})
                          if m in ("get", "post", "put", "patch", "delete")))
    print(f"  {mark} [{where:7}] {ops:12} {path}")
sys.exit(1 if missing else 0)
