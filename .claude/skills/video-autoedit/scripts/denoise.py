"""用 DeepFilterNet3 把指定素材的音訊降噪，產生 <job>/denoised/<素材>.wav。

用法（在專案根目錄執行）:
    python audio_check.py --job jobs/<job>            # 先看哪幾支需要
    python denoise.py --job jobs/<job> --sources A001 A002 A003
    python render.py --job jobs/<job>                 # 片段會自動改用降噪過的音軌

降噪要放在**語音辨識之後**。whisper 系列本來就是在吵雜資料上訓練的，前級降噪
反而會讓辨識變差，而且降得越多越差。所以辨識一律吃原始音訊，降噪只影響成片。

需要額外安裝（可選功能，不裝就用成片音訊鏈預設的 afftdn）:
    pip install deepfilternet torchaudio
第一次執行會自動下載模型（約 10 MB）。CPU 上約 15 倍即時，18 分鐘素材約 1 分鐘。
"""
import argparse
import subprocess
import sys
import types
import wave
from pathlib import Path

import numpy as np

from common import read_json

SR = 48000
CHUNK, OVERLAP = 60.0, 0.5      # 分段處理，段與段之間交叉淡接，避免接縫


def load_model():
    """載入 DeepFilterNet3。它還在 import torchaudio 已經移除的模組，要先補回去。"""
    try:
        import torchaudio  # noqa: F401
    except ImportError:
        sys.exit("沒有安裝 torchaudio，請先執行：pip install deepfilternet torchaudio")
    if "torchaudio.backend.common" not in sys.modules:
        mod = types.ModuleType("torchaudio.backend")
        com = types.ModuleType("torchaudio.backend.common")

        class AudioMetaData:      # torchaudio 2.x 已移除，DFN 只拿它做型別註記
            def __init__(self, *a, **k):
                pass

        com.AudioMetaData = AudioMetaData
        mod.common = com
        sys.modules["torchaudio.backend"] = mod
        sys.modules["torchaudio.backend.common"] = com
    try:
        from df.enhance import enhance, init_df
    except ImportError:
        sys.exit("沒有安裝 DeepFilterNet，請先執行：pip install deepfilternet torchaudio")
    model, state, _ = init_df()
    if state.sr() != SR:
        sys.exit(f"模型取樣率是 {state.sr()}，這支腳本假設 48 kHz")
    return model, state, enhance


def read_audio(path: str) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
                          "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def run(model, state, enhance, x: np.ndarray, atten: float) -> np.ndarray:
    import torch
    step, ov = int(CHUNK * SR), int(OVERLAP * SR)
    out = np.zeros(len(x), dtype=np.float32)
    ramp = np.linspace(0, 1, ov, dtype=np.float32)
    pos = 0
    while pos < len(x):
        end = min(pos + step + ov, len(x))
        seg = torch.from_numpy(x[pos:end]).unsqueeze(0)
        y = enhance(model, state, seg, atten_lim_db=atten).squeeze(0).numpy()[: end - pos]
        if pos and len(y) >= ov:        # 和前一段交叉淡接
            out[pos:pos + ov] = out[pos:pos + ov] * (1 - ramp) + y[:ov] * ramp
            out[pos + ov:end] = y[ov:]
        else:
            out[pos:end] = y
        pos += step
        print(f"  {min(pos, len(x)) / SR / 60:.1f} / {len(x) / SR / 60:.1f} 分", end="\r", flush=True)
    return out


def write_wav(path: Path, x: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--sources", nargs="+", required=True, help="要降噪的素材代號")
    parser.add_argument("--atten", type=float, default=20.0,
                        help="最多壓掉幾 dB 的噪音（預設 20；壓越多越乾淨，但人聲也越容易失真）")
    args = parser.parse_args()

    sources = {s["id"]: s for s in read_json(args.job / "sources.json")["sources"]}
    missing = [sid for sid in args.sources if sid not in sources]
    if missing:
        sys.exit(f"sources.json 裡沒有這些素材：{missing}")

    model, state, enhance = load_model()
    out_dir = args.job / "denoised"
    out_dir.mkdir(exist_ok=True)
    for sid in args.sources:
        x = read_audio(sources[sid]["path"])
        before = 20 * np.log10(np.percentile(np.abs(x), 10) + 1e-9)
        print(f"{sid}：{len(x) / SR / 60:.1f} 分鐘")
        y = run(model, state, enhance, x, args.atten)
        after = 20 * np.log10(np.percentile(np.abs(y), 10) + 1e-9)
        write_wav(out_dir / f"{sid}.wav", y)
        print(f"{sid}：噪音底 {before:.1f} → {after:.1f} dB，已寫入 {out_dir / (sid + '.wav')}")
    print("\n接著重跑 render.py，片段會自動改用這些音軌（片段快取會失效並重新編碼）。")


if __name__ == "__main__":
    sys.exit(main())
