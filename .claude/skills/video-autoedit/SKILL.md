---
name: video-autoedit
description: 自動剪輯以說話為主的影片（訪談、教學、口播、Podcast 錄影，支援台語、華語、英語夾雜）：用 Breeze-ASR / whisper 取得逐字稿與時間戳，由 Claude 以 YouTube 導演的角度校稿、判斷內容、規劃剪輯腳本（EDL），再用 ffmpeg 剪接、刪停頓、燒入字幕、疊上標題／章節／金句／生字等字卡與配樂，產出可直接發布的成片。只要使用者提到剪片、剪訪談、粗剪、剪毛片、去掉 NG 或重錄、刪停頓或贅字、把長片剪短、上字幕、做字卡或人名條，或丟一個影片檔說「幫我剪」，就使用這個 skill，即使沒有明說「自動剪輯」。
---

# video-autoedit

你是這支影片的導演兼剪輯師：判斷內容、決定節奏與包裝。
`scripts/` 負責所有確定性的工作（辨識、換算時間、剪接、字幕、渲染）。

**分工原則**：你只產出檔案——`corrections-*.json`、`edl.json`、必要時的字卡模板——
而且**只引用句子 ID，不寫秒數**。手寫 ffmpeg 指令與秒數容易出錯又難察覺，交給程式。

指令都在專案根目錄執行。腳本在 `.claude/skills/video-autoedit/scripts/`（以下簡寫 `S`）；
Python 在 Windows 是 `.venv/Scripts/python.exe`，macOS／Linux 是 `.venv/bin/python`。
輸出中文的程式請設 `PYTHONIOENCODING=utf-8`。

## 一次性設定

```bash
python -m venv .venv
PY=.venv/Scripts/python.exe                       # macOS／Linux：.venv/bin/python
$PY -m pip install faster-whisper opencc-python-reimplemented numpy jieba
$PY -m pip install torch --index-url https://download.pytorch.org/whl/cpu
$PY -m pip install transformers                   # 轉檔 Breeze 模型才需要
$PY -m pip install deepfilternet torchaudio       # 可選：降噪
$PY S/setup_models.py breeze-25 breeze-26 large-v3
$PY S/make_stings.py                              # 合成片頭／章節／片尾音效
```

另需 ffmpeg（含 libass）、Chrome／Edge／Chromium（字卡轉圖）、思源黑體與宋體（Noto Sans／Serif TC）。

字型目錄、瀏覽器、顯卡編碼器（AMF／NVENC／QSV／VideoToolbox）都會自動偵測；語音辨識一律走 CPU。
**換平台後第一支片要完整跑一次品質檢查**——硬體編碼器各有各的毛病。

## 流程

以 `jobs/<job>/` 為工作目錄，每一步的產物都是檔案，可單獨重跑。

1. **登記素材** `S/probe.py <影片...> --job jobs/<job>`。注意 VFR 警告；訪談放前、空鏡放後，補素材時編號才不會變。
2. **語音辨識** `S/transcribe.py --job jobs/<job> --model breeze-25 --prompt "<人名、地名、專有名詞>"`（放背景）
   - 華語用 `breeze-25`；台語或口音重用 `breeze-26`；拿不準就兩個各跑一小段比較。
   - `--prompt` 能改善專有名詞，但偶爾會被抄進逐字稿開頭，校稿時刪掉。空鏡用 `--sources` 排除。
3. **逐字稿** `S/build_transcript.py --job jobs/<job> --model <同上>`，讀完整份 `transcript.md` 再動手。
4. **收音** `S/audio_check.py --job jobs/<job>`，建議降噪的素材跑 `S/denoise.py`（見「音訊」）。
5. **看畫面**：抽幾格實際看人物位置與空白處，決定推鏡中心與字卡面板放哪。
6. **校稿** → `corrections-*.json`，涵蓋所有會保留的句子。
7. **剪輯規劃** → `edl.json`（規格 `references/edl-schema.md`，準則 `references/directing.md`）。
8. **檢查** `S/validate_edl.py --job jobs/<job>` → `剪輯腳本.md`。使用者要先看腳本就停在這裡。
9. **字卡** `S/cards.py --job jobs/<job>`，拼成總覽圖看過。
10. **時間軸與字幕** `S/render.py --job jobs/<job> --plan-only`，再 `S/captions.py --job jobs/<job>`。
    一兩個字自成一條、以「的」開頭的字幕，代表要把邊界字移到隔壁句。
11. **渲染** `S/render.py --job jobs/<job>`（放背景）→ `final.mp4`、`final.srt`。片段有快取，只改字幕時只重跑合成。
12. **品質檢查**後才回報。

## 校稿

`corrections-*.json` 可分批寫，依檔名順序合併，後面的覆蓋前面的：

```json
{
  "A001-0012": "校正後的文字，含全形標點。",
  "A001-0013": {"text": "學而時習之，不亦說乎？", "style": "classic"},
  "A001-0014": ""
}
```

- 文字要和這一句**實際說出的內容**一致（字幕時間靠逐字對齊），只修錯字、補標點、刪無意義的贅詞。
- **標點決定字幕怎麼斷**，該斷的地方一定要有標點。
- 一個詞被拆在兩句之間時（「接」／「班」），把字移到同一句。
- `style: "classic"`：古文或詩詞原文，用宋體。朗讀段辨識錯太多時直接換成原文。
- 空字串：這句不上字幕。

## 空鏡（B-roll）

- 空鏡每 2～3 秒抽一格拼成總覽圖（`fps=0.4,drawtext=…,tile=6x4`），挑出可用的鏡頭與起點，
  避開晃動、失焦、倒置、有工作人員入鏡的片段。
- 用 `type: "broll"` 的 overlay 蓋在訪談上，一段 3～8 秒，受訪者講到作品、技法、地點時切。
- 進出會自動溶接；空鏡前後不到 0.8 秒的訪談碎片會自動併進空鏡。

## 字卡與配樂

- 共用模板在 `assets/cards/`：`title`、`chapter`、`question`、`end`（全畫面），`quote`、`lower_third`（疊加）。
  **每支影片都可以設計專用模板**，放在 `jobs/<job>/templates/`（寫法見 `references/cards.md`）。
- 疊加面板要避開人臉；設了 `plate_width` 字幕會自動讓開。
- 字卡的 `music` 依序找 `<job>/audio/` → 專案的 `music/` → `assets/audio/`。
  `music/index.tsv` 列出曲庫的類型與情境，依影片情緒挑，EDL 寫曲名即可。字卡配樂比對白低 2～3 dB 最自然。
- `music` 目前只能掛在字卡上；整段鋪底的背景音樂還不支援。

## 輸出

預設 2560×1440 H.265、顯卡編碼；素材比 1440p 大時跟著素材走。升採樣不增加細節，
但 YouTube 對 1440p 以上的上傳分配較高位元率。顯卡比 CPU 快一個數量級，代價是同畫質位元率多約四成；
要留存檔母帶用 `--cpu-final`。

## 音訊

- **降噪只能放在語音辨識之後、只作用在成片。** whisper 系列本來就是用吵雜資料訓練的，
  前級降噪會讓辨識變差，而且降得越多越差。
- `audio_check.py` 依訊噪比判斷：30 dB 以上乾淨；20～30 dB 用預設的 afftdn 即可；
  低於 20 dB 建議 `denoise.py`（DeepFilterNet3，`--atten` 控制強度，預設 20 dB）。
  產生的 `denoised/<素材>.wav` 會被 `render.py` 自動採用，並略過 afftdn。
- 成片音訊鏈：highpass →（afftdn）→ 固定增益 → 壓縮 → 混配樂 → 響度增益 → 限幅，目標 −14 LUFS。

## 品質檢查（回報前一定要做）

- 抽 5～8 格實際看：字卡、字幕大小位置、推鏡、面板有沒有擋到人。字卡拼總覽圖檢查換行、溢出、錯字。
- 抽格用輸出端定位（`-i 檔案 -ss 秒數`）；`-ss` 放在 `-i` 前面，片頭會定位錯。
- **時間戳連續性**：`ffprobe -show_entries packet=pts,duration` 確認影像與音訊的封包都首尾相接，
  且實際解出的音訊長度等於宣告長度。有缺口的話播放時會突然快轉。
- 響度約 −14 LUFS（`ebur128`），音軌 `start_time` 為 0。
- 依「畫面來源＋推鏡」合併成鏡頭，最短不少於 1.2 秒。
- 切點前 0.1 秒若仍接近講話音量，可能切到字尾，加大 `pause.keep_after`。吵雜素材這個指標不準，改看字級時間戳。
- 回報：成片長度、刪了什麼、哪些判斷沒把握、哪些要使用者確認（人名、錯字、事實）。

## 踩過的坑

通用（ffmpeg／libass／whisper 本身的行為）：

- 字幕樣式名稱不能用小寫 `default`（libass 會套用內建的小字樣式）；ASS 檔不加 BOM、用 LF。
- 片段不能 `-c copy`（只能切在關鍵影格），也不能用 `-frames:v` 截斷（會丟尾端音訊），改用 `trim`／`atrim`。
- 串接清單要寫明每段的 `duration`，否則交界會多出空檔，畫面愈到後面愈慢。
- 空鏡與疊加圖要在片段階段就編進去，留到合成才疊會卡死；疊加圖不要位移時間戳。
- 音訊不能和影像放在同一個 `filter_complex`，ffmpeg 會隨機掉音訊畫格；音訊先單獨算成 wav。
- 不用 `loudnorm`（會改取樣率、時間戳有洞）和 `dynaudnorm`（吃掉開頭音訊），改用量測後的固定增益。
- 配樂先合成一條與成片等長的音軌再混，不要用 `adelay` + `amix`。
- 簡轉繁用 OpenCC `s2tw`，不要用 `s2twp`（會改詞彙）；`于` 仍會變成 `於`，古文要校回來。
- whisper 常省略「嗯、呃」，停頓要靠音量判斷，不靠逐字稿。

AMD AMF 編碼器特有（換平台要重驗）：遇到循環的圖片輸入會整段凍結畫格，
所以有疊加圖的片段改用 CPU 編碼（程式只在 AMF 上這樣做）。
