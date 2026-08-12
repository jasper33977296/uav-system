# 自駕儀驅動介面規格（issue 026 B1）

- 狀態：**定義完成，尚無實作**（2026-08-12）
- 契約程式碼：`libs/autopilot/driver.py`
- 架構決策與 QGC 對照：`doc/autopilot-driver-architecture.md`

B1 是**純定義**：形狀先固定，B2／B3 才把 `apps/command/app/mav.py` 的
`dialect()` 與 `apps/backend/app/dialect.py` 搬進 `Px4Driver`／`ArduPilotDriver`。
先定義後搬遷是刻意的——搬運時是「提取一處」而不是邊搬邊改介面。

---

## 1. 驅動是無狀態的

方法一律把所需狀態當參數收；驅動自己**不持有機體狀態**。機體狀態留在
`backend/app/state.py` 與 command 端的 router。

與 QGC 的 `FirmwarePlugin` 同形——實測其標頭**沒有任何成員狀態**，方法一律收
`Vehicle*`：

```cpp
virtual void initializeVehicle(Vehicle* vehicle) {}
virtual bool isCapable(const Vehicle* vehicle, FirmwareCapabilities) const;
```

**為什麼**：一個廠牌一個驅動實例，但機體是多台。驅動若持有狀態，多機就會共用
到同一份，或被迫變成「每台一個驅動實例」——後者讓「這是 PX4 的行為」與「這是
第 3 號機的狀態」混在同一個物件裡，正是我們要拆開的兩件事。

> 2026-08-12 的 issue 028 正是這個混淆的實例：起飛序列拿**主機**的高度去判斷
> **目標機**到沒到。介面層面把兩者分開，是為了讓那類 bug 寫不出來。

## 2. 兩類方言，兩種方法

| 類別 | 方法 | 定義 |
|---|---|---|
| **訊息層** | `adjust_incoming`／`adjust_outgoing` | 同一件事、不同訊息名或欄位名。正規化成單一標準形後，下游完全不必知道廠牌 |
| **解讀層** | 其餘 11 個 | 同一個值、不同意義，必須知道語意才解得開 |

### 2.1 `adjust_*` 的判準（規格）

> **需要知道「值代表什麼意思」的，就不屬於訊息層。**

正規化只認結構、不認語意：**不得對映模式、不得算就緒、不得構造指令**。

這一刀是規格不是建議。沒有它，`adjust_*` 會變成什麼都往裡塞的垃圾抽屜，而那
等於把方言問題從各處搬到一個大函式裡——抽象效益歸零。

**配套機制**：`adjust_*` 必須用 `MESSAGE_ADJUSTMENTS` 明文宣告它碰哪些訊息型別，
並由測試釘住那份清單（同 `SEND_WHITELIST` 的手法）。往裡加東西就會測試失敗，
逼加的人回來面對「這到底該不該是 adjust」。

### 2.2 等價宣告必須帶適用範圍

**每個等價宣告都要能回答「在什麼範圍內成立」，不能只寫「A 等於 B」。**

B0 實測的血淋淋案例：`EKF_STATUS_REPORT` 與 `ESTIMATOR_STATUS` 的 flags **只有
bit 1..512 逐位同義**；bit 1024 在兩邊分別是 `ESTIMATOR_GPS_GLITCH` 與
`EKF_UNINITIALIZED`——**同位元、不同意義**。

「等價」不帶範圍時，危險的不是寫錯，是**寫的時候是對的、用的時候沒人記得
範圍**：日後有人讀第 1024 位，程式不會報錯，只會靜默給出錯的答案。

型別強制（`MessageEquivalence`）：

```python
MessageEquivalence("EKF_STATUS_REPORT", "ESTIMATOR_STATUS",
                   safe_field_bits={"flags": 0x03FF},
                   note="bit 1024 兩邊不同義（GPS_GLITCH vs UNINITIALIZED）")
```

## 3. 十三個成員

粗粒度是刻意的。QGC 在**同一業務範圍**內用了約 30 個方法，差別主要在粒度——
它把模式拆成 `flightMode`／`flightModes`／`setFlightMode` 加 11 個具名模式存取子，
我們用一張模式表涵蓋同一批知識。**未來要加動詞就加方法，不預先開空方法**；
QGC 那份清單（架構文件 §5.2）留著當菜單。

| 成員 | 職責 | 對應差異 |
|---|---|---|
| `adjust_incoming` / `adjust_outgoing` | 訊息層正規化 | 12（含 8） |
| `decode_mode` | `custom_mode` → 顯示名 | 2 |
| `encode_mode` | 動詞模式名 → `DO_SET_MODE` 的值 | 1 |
| `mode_matches` | 用 HEARTBEAT 驗證真的切過去了 | 1 |
| `takeoff_plan` | 起飛序列（GUIDED 前置／相對 vs 絕對高／空白參數慣例） | 3、9 |
| `manual_prepare` | 進手動前要先切的模式 | 4 |
| `mission_line` | 任務項線序慣例（home 是否佔 seq 0） | 5 |
| `on_connect` / `keepalive` | 連線初始化與定期補送 | 6 |
| `readiness_signals` | 本廠牌有哪些**權威**就緒訊號 | 7 |
| `limits` | 因機型而異的數值限制 | 11 |
| `capabilities` | 動詞四態 | — |

幾條容易寫錯的：

- **`mode_matches` 不可省略**：送出成功不等於切成功。
- **`readiness_signals` 沒有的訊號要如實缺席**，讓 `readiness()` 回 `None`
  （未知），而不是拿次級訊號（GPS 好）冒充權威判斷。ArduPilot 不回報 PREARM。
- **`keepalive` 不是可有可無**：串流率設在自駕儀端，機端重開機、換通道、我方
  重連之後就沒了——只在連線時送一次的話，那些情況會**靜默失去全部遙測**
  （只剩心跳，看起來還「連著」）。

## 4. `limits()` 是對外契約，不是內部常數

消費者：UI 決定輸入框的 min／max／預設（`ui-spec` §0.2c 條款 5b）、MCP 避免
送出必然被拒的值。

```python
Limit(min=2.5, max=None, default=10.0,
      confidence="sitl", source="px4-param:MIS_TAKEOFF_ALT")
```

### 4.1 三級 `confidence`

| 值 | 意思 | UI 行為（UI/UX 2026-08-12 定案） |
|---|---|---|
| `sitl` | SITL 實測驗過 | 套用，可標示驗證基準 |
| `doc` | 依韌體文件／參數預設，未實測 | 套用，可提示證據強度較弱 |
| `unverified` | **我們不知道** | **不套用任何數字**，佔位提示、送出前必填 |

**`unverified` 時 `min`／`max`／`default` 必須皆為 `None`**——建構期檢查強制
（`Limit.__init__` 會 raise）。不允許「有數字但標未驗證」這種組合，否則 UI 會
面臨「要不要用這個數字」的判斷，而那不該是 UI 的判斷。

### 4.2 為什麼不做「顯示寫死值但標示未驗證」的過渡

UI/UX 選了最嚴的一邊，三個理由：

1. **那正是我們剛消滅過的形狀**——標籤在、內容是幻影（`droneLayer.ts` 的假註解、
   徽章顯示「—」等於形同移除）。**標了「未驗證」但仍給出 10，使用者會照用 10。**
2. **這是飛安參數**。體驗變差（要自己填）與在未知廠牌上用一個沒依據的高度起飛，
   代價不對等。
3. **有摩擦才會有人去驗**。UI 幫忙遮住「我們不知道」，B3 一致性測試就永遠不急。
   **讓不知道被看見，是讓它被解決的前提。**

### 4.3 首批只放一個欄位

`takeoff_alt_m`。**有真實消費者的才放**——速度上下限、空速限制在 QGC 有，但我們
目前沒有任何 UI 或指令在用，放了就是沒人維護的死欄位，會漂移成謊言。

欄位名帶單位（`_m`），避免「這個 10 是公尺還是英尺」的整類問題（同
`retention_days`）。

**現況待修的實例**：起飛高度在 `apps/command/app/main.py` 是全域預設 `10.0`
不分廠牌；前端寫死 min 3／max 100／預設 10。B2 落地後兩邊都改由 `limits()` 供給。
在 B3 實測之前，各廠牌多半只能誠實標 `unverified`——**不照抄 QGC 的數字當我們
的事實，那等於把別人的驗證當成我們的驗證**。

## 5. `capabilities()` 的值從哪來

**一致性測試結果 ∩ 執行期前提檢查**（B3 落地）：

- `ok` 當且僅當「驅動通過該動詞的一致性測試」**且**「這台機的執行期前提滿足」
- 例：ArduPilot 的手動控制要 `SYSID_MYGCS` 指向我方（差異 10）

把兩者混為一談會得到兩種錯誤——用測試結果宣告一台沒設好的機可用，或用單機
設定否定驅動本身的正確性。

一致性測試跑在 SITL，**SITL ≠ 真機**：能力宣告的準確語意是「對 <韌體版本> 的
SITL 驗證通過」，不是「真機保證可用」。

## 6. B2／B3 待辦

- **B2**：兩端實作搬進 `libs/autopilot/`；`libs/` 進 repo 根 build context；
  差異 8 改由 `adjust_incoming` 正規化而非解碼時分支；`limits()` 接上 API 與 UI
- **B3**：一致性測試套；`capabilities()` 改由測試結果推導；`MESSAGE_ADJUSTMENTS`
  清單的釘樁測試
