# auto-video-editor

讓 Claude 自動剪輯以說話為主的影片（訪談、口播、教學），包裝成 Claude Code skill（`video-autoedit`）。
在這個資料夾開 Claude Code，skill 會自動載入；把素材放進 `samples/`，說「幫我剪」即可。

成品可直接發布或放映：刪 NG 與長停頓、跳剪推鏡、空鏡（含投影片／照片）、燒入字幕（另附 SRT）、
標題／章節／人名條／金句字卡、字卡音效與鋪底配樂、降噪與音量平衡。

## 流程

```
素材 ─▶ probe            登記素材、抽辨識音訊（長錄影可只切出要用的段落）
     ─▶ transcribe       Breeze-ASR／whisper（faster-whisper，CPU）→ 字級時間戳
     ─▶ build_transcript 斷句、繁體化、編句子 ID → transcript.md
     ─▶ Claude 校稿      corrections-*.json
     ─▶ Claude 剪輯規劃  edl.json：保留哪些句子、章節、字卡、空鏡、配樂
     ─▶ validate_edl     檢查 → 剪輯腳本.md
     ─▶ cards            HTML 模板 → 無頭瀏覽器 → PNG
     ─▶ captions         依標點斷行、對到成片時間
     ─▶ render           各素材降噪與音量對齊 → 逐段編碼 → 串接 → 混音 → 燒字幕 → final.mp4
     ─▶ qc               影音同步、響度、配樂音量、字幕、切點 → 畫面總覽圖
```

核心原則：**Claude 負責判斷，程式負責執行**。Claude 只引用句子 ID，不寫秒數。詳見 `SKILL.md`。

## 需求

- Python 3.12、ffmpeg（含 libass）、Chrome／Edge／Chromium、思源黑體與宋體
- 辨識模型用 `setup_models.py` 下載（Breeze-ASR-25／26、whisper large-v3，各約 3 GB，int8 約 1.5 GB）；
  預設放在 `models/`，可用環境變數 `AUTOEDIT_MODELS` 改到其他位置（例如 NAS）
- 顯卡編碼可選：AMD AMF、NVIDIA NVENC、Intel QSV、Apple VideoToolbox（沒有就用 CPU）

安裝步驟見 `.claude/skills/video-autoedit/SKILL.md` 的「一次性設定」。

## 配樂（可選）

`music/` 放你自己的曲庫，音檔不進版控。沒有曲庫也能用：字卡音效會改用內建的合成音效，只是不會有整段鋪底的背景音樂。

- 檔名用「曲名 - 演出者.mp3」，EDL 只要寫曲名就找得到。YouTube 工作室音效庫下載的檔案就是這個格式。
- `music/index.tsv` 是曲目對照表，Claude 依其中的「類型」「情境」替影片挑曲。目前的內容是作者的曲庫，
  請換成你手上實際有的曲子：一行一首，以 Tab 分隔「曲名、類型、情境、演出者、檔名」五欄。
- 授權請自行確認。YouTube 音效庫的曲子可用在 YouTube 影片，部分要求在說明欄標註出處；
  用在其他場合前請先看各曲的授權條款。

## 目錄

```
.claude/skills/video-autoedit/
├── SKILL.md          skill 主體：流程、校稿、素材處理、品質檢查
├── scripts/          所有確定性的工作
├── references/       EDL 規格、導演準則、字卡模板寫法
└── assets/           共用字卡模板、合成的片頭／章節／片尾音效
jobs/<job>/           每支影片的工作目錄           （不進版控）
samples/  models/     素材、辨識模型               （不進版控）
music/                自備曲庫（音檔不進版控，只收 index.tsv 曲目表）
output/               成品                         （不進版控）
```

## 已知限制

- 辨識只走 CPU（約 1 倍即時）。GPU 方案只給句級時間戳，這套流程需要字級時間戳。
- 剪輯單位是句子，加上自動刪長停頓；字級剪除（刪「嗯、那個」）尚未實作。
- 多機位同步、說話者分離尚未實作。
- 主要在 Windows + AMD 顯卡上驗證；macOS／Linux 已處理路徑與編碼器，但未實機測試。
