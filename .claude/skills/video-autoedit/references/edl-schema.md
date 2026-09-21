# EDL（剪輯決策檔）規格 v1

`edl.json` 是 Claude 與渲染程式之間的介面：Claude 寫它，`validate_edl.py` 檢查它，`cards.py`、`render.py`、`captions.py` 執行它。

## 設計原則

1. **引用句子 ID，不寫秒數**。ID 來自 `transcript.md`，精確時間由程式從字級時間戳換算。
   唯一允許的秒數是字卡的 `duration` 與微調用的 `nudge`。
2. **每個決定都附理由**。`reason` 會出現在「剪輯腳本.md」。
3. **每一句都要有交代**：每個句子 ID 要嘛在 `timeline` 裡被保留，要嘛落在 `dropped` 的範圍內。
   驗證程式會列出兩邊都沒有的句子。

## transcript.md（Claude 讀的輸入）

```
## A001 · A001.MP4 · 12:31.0
A001-0001 00:03.2 大家好今天要來聊聊自動剪片
A001-0002 00:07.9 我重來 ⚠
          ⏸ 3.4s
A001-0003 00:12.5 大家好今天我們要來聊聊怎麼讓AI自動剪片
```

- ID 格式：`<素材代號>-<四位流水號>`；時間只列起始（mm:ss.s）
- `⏸`：超過 1 秒的停頓；`⚠`：辨識信心低

## 範例

```json
{
  "version": 1,
  "title": "某訪談片",
  "output": {"punch_in": {"zoom": 1.1, "center": [0.45, 0.4]}},
  "style": {"theme": "night", "accent": "#8A5A14"},
  "timeline": [
    {"type": "clip", "source": "A001", "from": "A001-0102", "to": "A001-0105",
     "reason": "開場鉤子：最有張力的一句"},
    {"type": "card", "template": "title", "duration": 4.5, "music": "sting_title",
     "fields": {"series": "系列名稱", "title": "片名"}},
    {"type": "clip", "source": "A001", "from": "A001-0002", "to": "A001-0052",
     "reason": "入行經過", "nudge": {"end": 0.3}}
  ],
  "dropped": [
    {"from": "A001-0106", "to": "A001-0113", "reason": "離題"}
  ],
  "overlays": [
    {"type": "lower_third", "at": "A001-0002", "duration": 6,
     "fields": {"name": "受訪者姓名", "title": "職稱"}},
    {"type": "broll", "source": "A007", "in": 30.0, "at": "A001-0042", "duration": 5}
  ]
}
```

## 欄位

### `output`（可省略，使用預設值）

| 欄位 | 預設 | 說明 |
|---|---|---|
| `width`、`height` | 2560、1440 | 輸出解析度；來源比例不同時會補黑邊。素材比 1440p 大時會自動改用素材的解析度（會處理 90/270 度旋轉），不會被默默降級 |
| `codec` | `h264` | `h264`（相容性最好）或 `hevc`（同畫質約省 3 成位元率，但 Windows 要裝 HEVC 擴充功能才能播） |
| `fps` | `30000/1001` | 輸出幀率（固定幀率） |
| `loudness_lufs` | -14 | 響度目標（YouTube 約 -14） |
| `punch_in.zoom` | 1.12 | 跳剪時交替放大的倍率 |
| `punch_in.center` | `[0.5, 0.45]` | 放大中心（0～1），設在人物中心 |
| `pause.max` | 1.2 | 超過幾秒的停頓要縮短 |
| `pause.keep_after`、`keep_before` | 0.45、0.25 | 縮短後在前後各留多少秒（keep_after 太小會吃掉字尾）|

### `style`

`theme`：`paper`（紙本）或 `night`（夜談）；`accent`：主色。見 `cards.md`。
`caption_scale`：選填，字幕放大倍率（預設 1.0＝58 px；外框與陰影一起縮放）。投影播放或觀眾距離遠時可設 1.1～1.2。

### `timeline[]`（依播放順序）

| type | 欄位 | 說明 |
|---|---|---|
| `clip` | `source` | 素材代號，對應 `sources.json` |
| | `from` / `to` | 起訖句子 ID（含頭含尾），同一素材 |
| | `reason` | 保留理由（必填） |
| | `tighten` | 選填，預設 true；false 表示不刪這段裡的長停頓 |
| | `nudge` | 選填，`{"start": 秒, "end": 秒}`，正值往後、負值往前 |
| | `punch_in` | 選填，預設 true；設 false 表示這段永遠不推鏡。畫面本身已有燒死的字幕或字卡時要關掉，否則推鏡會把下緣裁掉 |
| `card` | `template` | 模板名稱：`<job>/templates/` 優先，其次共用模板 |
| | `duration` | 秒數 |
| | `fields` | 模板欄位 |
| | `music` | 選填，配樂名稱。依序找 `<job>/audio/` → 專案的 `music/`（YouTube 音效庫曲庫，看 `music/index.tsv` 挑）→ `assets/audio/`（合成的撥弦音效）。寫曲名即可，不用寫演出者 |
| | `music_gain` | 選填，配樂增益（dB），預設 0 |
| | `fade` | 選填，進出場淡入淡出秒數，預設 0.3 |

### `dropped[]`

刪除的句子範圍與理由。沒把握的刪減註明「可保留，請使用者決定」，回報時列出。

### `overlays[]`

疊在影片上的字卡，`type` 就是模板名稱（例如 `quote`、`lower_third`，或這支影片的專用模板）。

| 欄位 | 說明 |
|---|---|
| `at` | 在哪一句開始時出現。**不要指向開場鉤子裡重複使用的句子**（會對到開場那一段） |
| `offset` | 選填，相對 `at` 句開頭再延後幾秒 |
| `duration` | 秒數；遇到下一張全畫面字卡會自動提早結束 |
| `fields` | 模板欄位；含 `plate_width` 時字幕會自動移到面板左側 |

### 空鏡（B-roll）

`type: "broll"` 的 overlay 會把畫面換成另一段素材，聲音仍是訪談（紀錄片最常用的手法）。

```json
{"type": "broll", "source": "A007", "in": 30.0, "at": "A001-0042", "duration": 5}
```

- `source`：空鏡素材代號（也要先用 `probe.py` 登記，不需要辨識）
- `in`：從空鏡素材的第幾秒開始用。這是看畫面挑的，**先抽影格看過再決定**，避開晃動、失焦、倒置的片段
- 進出自動溶接；空鏡在字卡與字幕之下，字幕照常顯示
- 一段空鏡 3～8 秒為宜；受訪者講到具體的作品、動作、地點時最適合切空鏡

## 程式如何換算時間（供理解，不需手算）

1. `from` 句第一個字的起點、`to` 句最後一個字的終點
2. 往外預留（起點 −0.12 秒、終點 +0.30 秒），再依音量找靜音處，避免切掉字頭字尾（不會跨進相鄰的句子）
3. 區間內超過 `pause.max` 的靜音縮短，片段因此切成數小段
4. 套用 `nudge`，所有切點對齊到輸出幀格線
5. 每個切點加 12 ms 音訊淡入淡出，避免爆音；跳剪處交替推鏡
