---
name: video-autoedit
description: 自動剪輯以說話為主的影片（訪談、教學、口播、演講與活動紀錄、Podcast 錄影，支援台語、華語、英語夾雜）：用 Breeze-ASR / whisper 取得逐字稿與時間戳，由 Claude 以導演的角度校稿、判斷內容、規劃剪輯腳本（EDL），再用 ffmpeg 剪接、刪停頓、燒入字幕、疊上標題／章節／金句／人名條等字卡、空鏡與配樂，產出可直接發布或放映的成片。只要使用者提到剪片、剪訪談、粗剪、剪毛片、去掉 NG 或重錄、刪停頓或贅字、把長片剪短、剪活動回顧、上字幕、做字卡或人名條，或丟一個影片檔說「幫我剪」，就使用這個 skill，即使沒有明說「自動剪輯」。
---

# video-autoedit

你是這支影片的導演兼剪輯師：判斷內容、決定節奏與包裝。
`scripts/` 負責所有確定性的工作（辨識、換算時間、剪接、字幕、渲染、品質檢查）。

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
$PY -m pip install deepfilternet torchaudio       # 可選：吵雜素材的降噪
$PY S/setup_models.py breeze-25 breeze-26 large-v3
$PY S/make_stings.py                              # 合成片頭／章節／片尾音效
```

另需 ffmpeg（含 libass）、Chrome／Edge／Chromium（字卡轉圖）、思源黑體與宋體（Noto Sans／Serif TC）。

字型目錄、瀏覽器、顯卡編碼器（AMF／NVENC／QSV／VideoToolbox）都會自動偵測；語音辨識一律走 CPU。
**換平台後第一支片要完整跑一次 `qc.py`**——硬體編碼器各有各的毛病。

## 流程

以 `jobs/<job>/` 為工作目錄，每一步的產物都是檔案，可單獨重跑。

1. **確認用途**：影片要上傳 YouTube，還是在活動現場放映？使用者沒講就先問。它決定輸出規格（見「輸出」），
   寫進 `edl.json` 的 `output` 後就不再改：試剪給使用者看的版本，就是最後交付的版本。
2. **登記素材** `S/probe.py <影片...> --job jobs/<job>`。注意 VFR 警告；訪談放前、空鏡放後，補素材時編號才不會變。
   長錄影只用其中幾段時寫成 `"路徑#00:12:00-00:18:30"`，只切出那段，辨識快很多。
3. **語音辨識** `S/transcribe.py --job jobs/<job> --model breeze-25 --prompt "<人名、地名、專有名詞>"`（放背景）
   - 華語用 `breeze-25`；台語或口音重用 `breeze-26`；拿不準就兩個各跑一小段比較。
   - `--prompt` 能改善專有名詞，但偶爾會被抄進逐字稿開頭，校稿時刪掉。空鏡用 `--sources` 排除。
   - 聽不清的關鍵句用另一個模型比對：`--model large-v3 --sources A003 --window 起 迄`（只印出、不寫檔）。
     兩邊不一致時回報使用者並附上兩種辨識結果。
4. **逐字稿** `S/build_transcript.py --job jobs/<job> --model <同上>`，讀完整份 `transcript.md` 再動手。
5. **看畫面**：每支素材抽幾格，決定推鏡中心與字卡面板放哪。推鏡會放大約 12%，
   人站在畫面邊緣、或畫面上有燒上去的字幕／logo 的素材設 `punch_in: false`。
6. **校稿** → `corrections-*.json`，涵蓋所有會保留的句子。
7. **剪輯規劃** → `edl.json`（規格 `references/edl-schema.md`，準則 `references/directing.md`）。
8. **檢查** `S/validate_edl.py --job jobs/<job>` → `剪輯腳本.md`。使用者要先看腳本就停在這裡。
9. **字卡** `S/cards.py --job jobs/<job>`，拼成總覽圖看過。
10. **時間軸與字幕** `S/render.py --job jobs/<job> --plan-only`，再 `S/captions.py --job jobs/<job>`。
    一兩個字自成一條、以「的」開頭的字幕，代表要把邊界字移到隔壁句。
11. **渲染** `S/render.py --job jobs/<job>`（放背景）→ `final.mp4`、`final.srt`。片段有快取，只改字幕時只重跑合成。
12. **品質檢查** `S/qc.py --job jobs/<job>`，處理完才回報（見「品質檢查」）。

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
  用 `nudge` 裁掉句子頭尾時，校稿也只寫保留下來的部分。
- **標點決定字幕怎麼斷**，該斷的地方一定要有標點。
- 一個詞被拆在兩句之間時（「接」／「班」），把字移到同一句。
- `style: "classic"`：古文或詩詞原文，用宋體。朗讀段辨識錯太多時直接換成原文（簡轉繁會把「于」變成「於」，要校回來）。
- 空字串：這句不上字幕。
- 辨識模型不支援的語言（例如客語）：文字不能用，但時間戳大致可信。使用者有逐字稿時，照每句的長度把逐字稿分進各句。
- 辨識漏掉的話（兩句之間明明有人講話卻沒有句子）上不了字幕：剪輯時避開，或回報使用者。

## 別人剪好的影片

官方活動影片、電視節目片段這類已經剪輯過的素材：

- 畫面上已有字幕：校稿全設空字串（不疊第二層字幕），並設 `punch_in: false`。
- 它有自己的鏡頭切換、片頭標題卡：用 `select='gt(scene,0.3)',showinfo` 找出切換點，片段起訖要避開，
  否則會閃到一兩格別的鏡頭或殘留標題卡。字級時間常偏早約 0.2 秒，貼近切換點時看音量決定 `nudge`。

## 空鏡（B-roll）

- 空鏡每 2～3 秒抽一格拼成總覽圖（`fps=0.4,drawtext=…,tile=6x4`），挑出可用的鏡頭與起點，
  避開晃動、失焦、倒置、有工作人員入鏡的片段。
- 用 `type: "broll"` 的 overlay 蓋在訪談上，一段 3～8 秒，講到作品、技法、地點時切。
- 進出會自動溶接（兩段空鏡相接則直接切換）；空鏡前後不到 0.8 秒的訪談碎片會自動併進空鏡。
- 靜態圖（投影片、照片）先用 `S/slideshow.py <圖或資料夾> --out jobs/<job>/img/slides.mp4` 做成影片再登記；
  投影片加 `--mode whole`，推鏡才不會切到貼邊的字。每張的起訖在 `slides.json`，一段空鏡要整段落在同一張圖內。

## 字卡與配樂

- 共用模板在 `assets/cards/`：`title`、`chapter`、`question`、`end`（全畫面），`quote`、`lower_third`（疊加）。
  **每支影片都可以設計專用模板**，放在 `jobs/<job>/templates/`（寫法見 `references/cards.md`）。
- 疊加面板要避開人臉；設了 `plate_width` 字幕會自動讓開。
- 配樂依序找 `<job>/audio/` → 專案的 `music/` → `assets/audio/`；`music/index.tsv` 列出曲庫的類型與情境，
  依影片情緒挑，EDL 寫曲名即可。
  - 字卡音效（card 的 `music`）比對白低 2～4 dB 最自然。
  - 整段鋪底用 `bgm`（講話時可自動壓低），比人聲低約 15 dB。挑沒有人聲、起伏小、起音少的曲子。

## 輸出

規格依用途在開工時決定（流程第 1 步），寫在 `edl.json` 的 `output`：

- **上傳 YouTube（預設）**：2560×1440 HEVC。升採樣不增加細節，但 YouTube 對 1440p 以上的上傳改用
  VP9／AV1 並分配較高位元率，觀眾用 1080p 看也比較清楚。素材比 1440p 大時跟著素材走。
- **現場放映、給別人的電腦播放**：`"width": 1920, "height": 1080, "codec": "h264"`，到哪都能播
  （HEVC 在 Windows 要另裝擴充功能）。

中途改解析度要重跑 `cards.py`（字卡是照成片尺寸產生的，render 會檢查）。顯卡編碼比 CPU 快一個數量級，
代價是同畫質要多約四成位元率，所以 HEVC 用較低的 QP 補回來；要留存檔母帶用 `--cpu-final`。

## 音訊

渲染前 `render.py` 會自動整理每支素材的對白（`prep_audio.py` → `prepared/`）：降噪（訊噪比低於 20 dB
且裝了 DeepFilterNet 用 DFN3，否則 afftdn）、補償降噪造成的延遲、各素材先對齊到 −20 LUFS，
成片再整體標準化到 −14 LUFS。辨識一律吃原始音訊——前級降噪會讓辨識變差。

印出「削波嚴重」的素材救不回來，回報時要提。要指定降噪方式：`S/prep_audio.py --job jobs/<job> --sources A002 --denoise dfn`。

## 品質檢查（回報前一定要做）

`S/qc.py` 檢查規格、時間戳連續性、影音同步、響度、配樂音量、字幕、鏡頭長度、切點，
並把每個字卡、人名條與幾個取樣點拼成 `qc/overview.png`。⚠ 的項目處理完再回報。

- **總覽圖一定要打開看**：人名條是不是對的人、字幕與面板有沒有擋到臉、推鏡有沒有裁到人或字。
- 「刪掉的停頓裡有明顯聲音」：可能刪到很輕的字，那段設 `tighten: false`。
- 自己抽格：成片用 `select=eq(n\,幀號)` 最準；長素材的中段用 `-ss 秒數 -i 檔案`（放在 `-i` 前才快）。
- 回報：成片長度、刪了什麼、哪些判斷沒把握、哪些要使用者確認（人名、錯字、事實）。

## 改程式之前

`scripts/` 裡很多寫法是為了繞開 ffmpeg／libass／whisper 的已知問題（例如片段不能 `-c copy`、
音訊不能和影像放在同一個 `filter_complex`），原因都寫在註解裡；改之前先讀，改完用 `qc.py` 驗證。
