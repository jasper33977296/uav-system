"""PX4 EVENT（msg 410）→ 人話（issue 014）。

PX4 1.14 之後機上的 vehicle 通知走 Events 協定、**不走 STATUSTEXT**，訊息裡只有
event id ＋參數位元組。使用者原話是「機上事件看不出來發生啥事」——這支就是那句
話的修法：把 `event_id=30121562` 變成 `Battery unhealthy`。

字典與版本綁定說明見 `reference/px4-events/README.md`。三條規則：

1. **id 不是字典鍵**：`wire_id = (component << 24) | 字典鍵`。實測直接查表 0/10
   命中，套上這個關係 9/9（那份 README 記了為什麼）。
2. **翻不出就保留 raw**，不丟棄、不猜。事件本身（id／時間／severity）永遠比
   「翻不出來」重要——它至少證明機上發生了某件事。
3. **版本對不上寧可不翻**：事件 id 是名稱的雜湊，跨韌體版本可能改變。拿錯版
   字典翻出來的是「看起來合理但完全錯誤」的句子，比顯示 raw id 危險得多——
   讀的人不會懷疑它。
"""
import json
import logging
import pathlib
import re
import struct

log = logging.getLogger(__name__)

#: 字典檔（釘在 repo，不走 MAVLink FTP——本階段刻意的範圍決定）
_DICT_PATH = pathlib.Path(
    "/srv/reference/px4-events/all_events-v1.14.3.json")
#: 這份字典對應的韌體版本。**出現在每一則翻譯結果裡**，讓讀的人知道證據來源。
DICT_FW = "1.14.3"

_ARG_FMT = {                      # MAVLink events 的參數型別 → struct 格式
    "uint8_t": ("<B", 1), "int8_t": ("<b", 1),
    "uint16_t": ("<H", 2), "int16_t": ("<h", 2),
    "uint32_t": ("<I", 4), "int32_t": ("<i", 4),
    "uint64_t": ("<Q", 8), "int64_t": ("<q", 8),
    "float": ("<f", 4), "double": ("<d", 8),
}
#: 模板佔位符：`{1}`、`{2m_v}`、`{1:.1m/s}`——數字後面可帶
#: **格式規格（`:.1`＝小數位）＋單位**（`m`／`m_v`／`m/s`）。實測字典裡的分佈：
#: 無後綴 107、`m_v` 6、`:.1` 4、`:.1m/s` 2、`m` 2、`:.0m_v` 2 …
#: `_v` 是「垂直方向」的語意標記，**顯示時就是 m**（QGC 也這樣呈現）。
_PLACEHOLDER = re.compile(r"\{(\d+)(?::\.(\d))?([a-zA-Z/_]*)\}")
#: 單位提示 → 顯示字串。**不在表內的後綴一律不顯示**——寧可少一個單位，
#: 也不要憑猜測給出一個錯的單位（那會讓讀的人以為數字是別的量綱）。
_UNITS = {"": "", "m": " m", "m_v": " m", "m/s": " m/s"}
#: 描述裡的 `<profile name="dev">…</profile>`、`<param>…</param>` 等標記
_TAG = re.compile(r"<[^>]+>")

_events: dict = {}        # (component, key) → {message, arguments, group}
_enums: dict = {}         # (component, enum_name) → {value → description}
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True                     # 只嘗試一次；失敗就一直走 raw
    try:
        data = json.loads(_DICT_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("PX4 事件字典讀不到（%s）——事件維持顯示 raw id：%s",
                    _DICT_PATH, e)
        return
    for cid, comp in (data.get("components") or {}).items():
        c = int(cid)
        for ename, en in (comp.get("enums") or {}).items():
            _enums[(c, ename)] = {
                int(k): (v.get("description") or v.get("name") or str(k))
                for k, v in (en.get("entries") or {}).items()}
        for gname, g in (comp.get("event_groups") or {}).items():
            for k, ev in (g.get("events") or {}).items():
                _events[(c, int(k))] = {"group": gname, **ev}
    log.info("PX4 事件字典：%d 個事件、%d 組 enum（韌體 %s）",
             len(_events), len(_enums), DICT_FW)


def _fmt_value(v) -> str:
    if isinstance(v, float):
        return f"{v:.1f}".rstrip("0").rstrip(".")
    return str(v)


def _decode_args(comp: int, spec: list, blob: bytes) -> list:
    """按參數型別逐一解出。長度不足就停——**寧可少翻幾個參數，不要亂解位元組**。"""
    out, off = [], 0
    for a in spec or []:
        t = a.get("type", "")
        if t in _ARG_FMT:
            f, n = _ARG_FMT[t]
            if off + n > len(blob):
                break
            out.append(struct.unpack_from(f, blob, off)[0])
            off += n
        else:                          # enum（型別名就是 enum 名）
            table = _enums.get((comp, t))
            if off + 1 > len(blob):
                break
            raw = blob[off]
            off += 1
            out.append(table.get(raw, raw) if table else raw)
    return out


def describe(event_id: int, args_hex: str = "") -> dict | None:
    """回 {'text', 'event_name', 'group', 'dict_fw'}；翻不出回 None。

    呼叫端**必須保留原本的 event_id**——這裡回 None 時事件不能消失。
    """
    _load()
    if not _events:
        return None
    comp, key = event_id >> 24, event_id & 0xFFFFFF
    ev = _events.get((comp, key))
    if ev is None:
        return None
    msg = ev.get("message") or ev.get("name") or ""
    if not msg:
        return None
    try:
        blob = bytes.fromhex(args_hex or "")
    except ValueError:
        blob = b""
    vals = _decode_args(comp, ev.get("arguments"), blob)

    def _sub(m):
        i = int(m.group(1)) - 1        # 模板是 1-based
        if not (0 <= i < len(vals)):
            return m.group(0)          # 參數不足 → 保留原樣，不要生出假數字
        v, prec, unit = vals[i], m.group(2), m.group(3) or ""
        if prec is not None and isinstance(v, (int, float)):
            txt = f"{float(v):.{int(prec)}f}"
        else:
            txt = _fmt_value(v)
        return txt + _UNITS.get(unit, "")

    text = _TAG.sub("", _PLACEHOLDER.sub(_sub, msg)).strip()
    return {"text": text, "event_name": ev.get("name"),
            "group": ev.get("group"), "dict_fw": DICT_FW}
