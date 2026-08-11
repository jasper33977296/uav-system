"""`missions/` 目錄的 QGC `.plan` 航線檔：列表、讀取、解析成本系統的 waypoints。

解析規則與 `scripts/fly-mission.py::import_plan`、前端的 .plan 解析一致——
`.plan` 的原始 `command`/`frame`/`p1`–`p4` 全數保留，上傳到機時原樣送出
（MAVLink 保真度，2026-08-10 定案）。

外部端點會把使用者給的字串當檔名餵進來，`resolve()` 因此把它當敵意輸入處理：
只認 `missions/` 這一層的 `.plan`，路徑穿越一律拒絕。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

# 帶座標的導航類 MAV_CMD（與 plan_check 同一組定義）
NAV_CMDS = {16, 17, 18, 19, 20, 21, 22}
_ACTION = {22: "takeoff", 21: "land", 20: "rtl"}


class PlanError(Exception):
    """檔案不存在、不是 .plan、或內容不足以構成一條航線。訊息可直接給操作員。"""


def resolve(missions_dir: str, name: str) -> Path:
    """檔名 → 實際路徑。副檔名可省略。

    只接受 `missions/` **這一層**的檔案：`name` 含路徑分隔或指向目錄外
    （`../`、絕對路徑、symlink 指出去）一律拒絕。
    """
    if not name or name.strip() != name or "/" in name or "\\" in name:
        raise PlanError(f"航線檔名不合法：{name!r}（只接受 missions/ 下的檔名）")
    if not name.endswith(".plan"):
        name += ".plan"
    root = Path(missions_dir).resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file():
        raise PlanError(f"找不到航線檔「{name}」")
    return path


def parse(path: Path) -> dict:
    """`.plan` → {waypoints, home, nav_count, skipped, …}。

    `waypoints` 就是 `POST /api/missions` 與 `build_items()` 吃的格式。
    """
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise PlanError(f"讀取失敗：{e}")
    except json.JSONDecodeError as e:
        raise PlanError(f"不是合法的 JSON：{e}")
    if plan.get("fileType") != "Plan":
        raise PlanError("不是 QGC 的 .plan 檔（fileType != \"Plan\"）")

    mission = plan.get("mission") or {}
    wps: list[dict] = []
    skipped: list[str] = []
    for it in mission.get("items") or []:
        # 複雜項目（Survey/CorridorScan…）本系統不展開，跳過並回報——
        # 靜默略過會讓上傳的航線與檔案內容不一致，操作員無從察覺
        if it.get("type") != "SimpleItem":
            skipped.append(str(it.get("type")))
            continue
        p = list(it.get("params") or []) + [None] * 7
        cmd = it.get("command")
        wps.append({
            "seq": len(wps),
            "lat": p[4] or 0, "lon": p[5] or 0,
            "alt": it.get("Altitude") or p[6],
            "action": _ACTION.get(cmd, "waypoint" if cmd in NAV_CMDS else "do"),
            "command": cmd, "frame": it.get("frame"),
            "p1": p[0], "p2": p[1], "p3": p[2], "p4": p[3],
        })

    nav = [w for w in wps if w["command"] in NAV_CMDS and w["lat"] and w["lon"]]
    if len(nav) < 2:
        raise PlanError("檔案內找不到足夠的導航航點（需要至少 2 個帶座標的）")

    return {
        "name": path.name,
        "waypoints": wps,
        "item_count": len(wps),
        "nav_count": len(nav),
        "home": mission.get("plannedHomePosition"),   # [lat, lon, alt_msl]
        "cruise_speed": mission.get("cruiseSpeed"),
        "hover_speed": mission.get("hoverSpeed"),
        "skipped": skipped,
    }


def _stat(path: Path) -> dict:
    st = path.stat()
    return {
        "name": path.name,
        "size_bytes": st.st_size,
        "modified_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
    }


def scan(missions_dir: str) -> list[dict]:
    """總表：目錄下每個 `.plan` 一列。

    **解析失敗的檔一樣列出來**（`error` 欄帶原因）。過濾掉等於讓壞檔在總表上
    消失，外部要到觸發那一刻才發現——寧可在列表就看得見。
    """
    root = Path(missions_dir)
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.glob("*.plan")):
        row = _stat(path)
        try:
            d = parse(path)
            row |= {"item_count": d["item_count"], "nav_count": d["nav_count"],
                    "home": d["home"], "skipped": d["skipped"], "error": None}
        except PlanError as e:
            row |= {"item_count": None, "nav_count": None, "home": None,
                    "skipped": [], "error": str(e)}
        out.append(row)
    return out


def detail(path: Path) -> dict:
    """檔案：單一 `.plan` 的解析結果＋檔案中繼資料。"""
    return _stat(path) | parse(path)


def raw(path: Path) -> dict:
    """QGC 原始 JSON（原樣回傳，給要自己解析或轉存的取用端）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise PlanError(f"讀取失敗：{e}")
