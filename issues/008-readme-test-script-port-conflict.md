# 008 · README 測試腳本用 14540，會與執行中的 backend 搶埠

- 狀態：open
- 嚴重度：low
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
