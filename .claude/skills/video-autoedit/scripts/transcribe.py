"""語音辨識：對 job 內每個素材產生字級時間戳。

用法（在專案根目錄執行）:
    python transcribe.py --job jobs/<job> --model breeze-25
    python transcribe.py --job jobs/<job> --model breeze-26 --prompt "人名 地名 專有名詞"
    python transcribe.py --job jobs/<job> --model large-v3 --sources A003 --window 60 80   # 交叉比對一小段

產生 <job>/words/<ID>.<model>.json：
    {"source", "model", "segments": [{"start", "end", "text", "words": [{"start", "end", "word", "prob"}]}]}

--window 只辨識那一段並印出文字，不寫檔：聽不清楚的關鍵句用第二個模型比對時用。

引擎是 faster-whisper（CPU int8）。模型放在 <models-dir>/<model>-ct2/，由 setup_models.py 準備。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

BASE_PROMPT = "以下是台灣人的中文對話逐字稿，使用繁體中文與全形標點。"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--model", default="breeze-25", help="breeze-25（華語，預設）/ breeze-26（台語）/ large-v3")
    parser.add_argument("--models-dir", type=Path, default=Path(os.environ.get("AUTOEDIT_MODELS", "models")))
    parser.add_argument("--prompt", default="", help="專有名詞或主題提示，會接在預設提示後面")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--sources", nargs="*", help="只辨識這些素材代號（預設全部）")
    parser.add_argument("--window", nargs=2, type=float, metavar=("START", "END"),
                        help="只辨識這段秒數並印出來（不寫檔），用來交叉比對")
    parser.add_argument("--threads", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--no-vad", action="store_true",
                        help="直接關掉語音活動偵測（殘響很重的現場收音有時會被 VAD 整段濾掉）")
    args = parser.parse_args()

    from faster_whisper import WhisperModel

    sources = json.loads((args.job / "sources.json").read_text(encoding="utf-8"))["sources"]
    model = WhisperModel(str(args.models_dir / f"{args.model}-ct2"), device="cpu",
                         compute_type="int8", cpu_threads=args.threads)
    prompt = f"{BASE_PROMPT}{args.prompt}"
    out_dir = args.job / "words"
    out_dir.mkdir(exist_ok=True)

    for src in sources:
        if "asr_audio" not in src or (args.sources and src["id"] not in args.sources):
            continue
        audio = str(args.job / src["asr_audio"])
        if args.window:
            a, b = args.window
            raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(a), "-to", str(b),
                                  "-i", audio, "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                                 capture_output=True, check=True).stdout
            # 短片段不過 VAD、不給前文，免得模型「補完」出不存在的字
            segments, _ = model.transcribe(np.frombuffer(raw, dtype=np.float32), language=args.language,
                                           beam_size=5, vad_filter=False, condition_on_previous_text=False,
                                           initial_prompt=prompt)
            for seg in segments:
                print(f"[{src['id']} {args.model}] {a + seg.start:7.2f}～{a + seg.end:7.2f}  {seg.text.strip()}",
                      flush=True)
            continue

        out = out_dir / f"{src['id']}.{args.model}.json"
        t0 = time.time()

        def run(vad: bool):
            segments, info = model.transcribe(
                audio,
                language=args.language,
                beam_size=5,
                vad_filter=vad,
                vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 200} if vad else None,
                word_timestamps=True,
                # 長片關閉前文條件，避免 whisper 陷入重複或幻覺迴圈
                condition_on_previous_text=False,
                initial_prompt=prompt,
            )
            rows = []
            for seg in segments:
                rows.append({
                    "start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip(),
                    "words": [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word,
                               "prob": round(w.probability, 3)} for w in (seg.words or [])],
                })
                elapsed = time.time() - t0
                print(f"[{src['id']}] {seg.end / 60:6.1f}/{info.duration / 60:.1f} 分  "
                      f"速度 {seg.end / max(elapsed, 1e-6):.2f}x  {seg.text.strip()[:30]}", flush=True)
            return rows, info

        result, info = run(not args.no_vad)
        # Silero VAD 偶爾會把整段殘響很重的現場收音判成「沒有語音」而回傳空結果，
        # 這時關掉 VAD 重跑一次
        if not result and not args.no_vad:
            print(f"[{src['id']}] VAD 判定整段都不是語音，改用不過濾重跑", flush=True)
            t0 = time.time()
            result, info = run(False)
        out.write_text(json.dumps({
            "source": src["id"], "model": args.model, "prompt": prompt,
            "duration": info.duration, "elapsed_sec": round(time.time() - t0, 1), "segments": result,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[{src['id']}] 完成 → {out}（{(time.time() - t0) / 60:.1f} 分鐘）", flush=True)


if __name__ == "__main__":
    sys.exit(main())
