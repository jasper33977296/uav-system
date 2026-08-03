# 008 · README 測試腳本用 14540，會與執行中的 backend 搶埠

- 狀態：closed
- 嚴重度：low
- 關閉：2026-08-03
- 位置：`README.md:65`
- 建立：2026-08-03

## 現象

README「測試場景：飛進干擾區」的腳本連 `udpin://0.0.0.0:14540`：

```python
d = System(); await d.connect('udpin://0.0.0.0:14540')
```

但 backend 跑起來時，它的 mavsdk_server 已經綁住這個埠：

```
UNCONN 0 0 0.0.0.0:14540 0.0.0.0:* users:(("mavsdk_server",pid=260451,fd=3))
```

兩者不能同時使用同一個 UDP 埠——而這個測試腳本的用途正是「backend 在跑的時候
飛一趟看資料流」，所以照 README 做必定衝突。

## 原因

文件寫作時沒有考慮 backend 與控制腳本同時存在的情境。
PX4 SITL 對 14540（onboard/API）與 14550（GCS）都會送 MAVLink，
控制指令走哪個埠都可以。

## 影響

照 README 操作會失敗或行為詭異（視哪個程序先綁到埠而定）。
這是新人第一次跑這個專案就會踩到的地方。

## 修法建議

README 的腳本改連 14550，並註明原因：

```python
# backend 佔用 14540，控制腳本走 QGroundControl 的 14550
d = System(); await d.connect('udpin://0.0.0.0:14550')
```

2026-08-03 的首次實測就是用 14550 完成的（起飛 → 進干擾區 → RTL 全程正常）。

更好的作法是把這段腳本收成 `scripts/test-flight.py`，
README 只留一行呼叫——順便解決腳本內嵌在 markdown 裡不好維護的問題。

## 解決方式

採後者：新增 `scripts/test-flight.py`，README 改為單行呼叫並說明埠號選擇的理由。

腳本相較 README 內嵌版本的改進：

- 連 **14550**，避開 backend 佔用的 14540
- 起飛前等 `is_global_position_ok and is_home_position_ok`
  （原版直接 `arm()`，pre-flight check 未過時會失敗）
- 航線改為「進干擾區 → 飛回起點 → 返航」，能同時觀察到劣化與**回升**兩個方向
  的事件；原版只飛進去就 RTL
- docstring 說明埠號衝突的來龍去脈，避免日後有人改回 14540

## 驗證

在 backend 執行中（其 mavsdk_server 綁著 14540）跑此腳本，
連線成功、未發生埠衝突，完整飛完一趟並產生 157 筆遙測與完整鏈路事件序列。

註：若同時開著 QGroundControl，它也會佔用 14550——兩者擇一使用，已寫在 docstring。
