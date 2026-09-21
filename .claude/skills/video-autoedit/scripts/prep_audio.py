"""整理每支素材的對白音軌：量收音、降噪、音量對齊，寫成 <job>/prepared/<ID>.wav。

render.py 渲染前會自動替缺少的素材跑一次；想先看收音報告、或指定降噪方式時再手動執行：
    python prep_audio.py --job jobs/<job> [--sources A001 A003] [--denoise auto|afftdn|dfn|none] [--force]

每支素材依序：
  1. 量收音（16 kHz 辨識音訊）：訊噪比、噪音起伏、削波。
  2. 降噪。auto：訊噪比低於 20 dB 且裝了 DeepFilterNet 就用 DFN3，否則用 afftdn（強度依訊噪比）。
     降噪只作用在成片；語音辨識一律吃原始音訊，前級降噪會讓辨識變差。
  3. 音量對齊到 −20 LUFS。成片的響度標準化只看整支片，素材之間若差很多，講者之間會忽大忽小。
  4. 延遲補償。頻譜降噪會讓聲音晚幾十毫秒，量出延遲後裁掉，音軌才和影像對得齊。

DeepFilterNet 為可選：pip install deepfilternet torchaudio（第一次執行自動下載約 10 MB 模型）。
"""
import argparse
import json
import re
import subprocess
import sys
import types
import wave
from pathlib import Path

import numpy as np

from common import read_json, write_json

SR = 48000
TARGET = -20.0          # 每支素材的對白響度（LUFS）；成片最後再整體拉到 −14
VERSION = 1             # 處理方式改變時遞增，讓舊檔重做


# ── 量收音 ───────────────────────────────────────────────────

def quality(wav16k: Path) -> dict:
    """訊噪比＝講話音量 − 背景噪音；噪音起伏大代表冷氣、車聲這類非穩態噪音；削波救不回來。"""
    with wave.open(str(wav16k)) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    n = int(sr * 0.02)
    db = 20 * np.log10(np.sqrt((x[: len(x) // n * n].reshape(-1, n) ** 2).mean(axis=1)) + 1e-9)
    floor, speech = np.percentile(db, 10), np.percentile(db, 95)
    per = int(10 / 0.02)
    floors = [np.percentile(db[i:i + per], 10) for i in range(0, max(1, len(db) - per), per)] or [floor]
    return {"snr": float(speech - floor), "drift": float(np.percentile(floors, 90) - np.percentile(floors, 10)),
            "clip": float((np.abs(x) > 0.995).mean() * 100)}


# ── 降噪 ─────────────────────────────────────────────────────

def dfn_shim() -> None:
    """DeepFilterNet 還在 import torchaudio 已移除的 torchaudio.backend，先補一個空殼才載得進來。"""
    if "torchaudio.backend.common" not in sys.modules:
        mod, com = types.ModuleType("torchaudio.backend"), types.ModuleType("torchaudio.backend.common")
        com.AudioMetaData = type("AudioMetaData", (), {"__init__": lambda self, *a, **k: None})
        mod.common = com
        sys.modules["torchaudio.backend"], sys.modules["torchaudio.backend.common"] = mod, com


def dfn_available() -> bool:
    try:
        import torchaudio  # noqa: F401
        dfn_shim()
        from df.enhance import enhance, init_df  # noqa: F401
        return True
    except ImportError:
        return False


def dfn_denoise(x: np.ndarray, atten: float = 20.0) -> np.ndarray:
    """DeepFilterNet3，分段處理並交叉淡接。"""
    dfn_shim()
    import torch
    from df.enhance import enhance, init_df
    model, state, _ = init_df()
    step, ov = 60 * SR, SR // 2
    out = np.zeros(len(x), dtype=np.float32)
    ramp = np.linspace(0, 1, ov, dtype=np.float32)
    for pos in range(0, len(x), step):
        end = min(pos + step + ov, len(x))
        y = enhance(model, state, torch.from_numpy(x[pos:end]).unsqueeze(0),
                    atten_lim_db=atten).squeeze(0).numpy()[: end - pos]
        if pos and len(y) >= ov:
            out[pos:pos + ov] = out[pos:pos + ov] * (1 - ramp) + y[:ov] * ramp
            out[pos + ov:end] = y[ov:]
        else:
            out[pos:end] = y
    return out


def decode(path: str, af: str) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path, "-vn", "-map", "0:a:0",
                          "-af", af, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def write_wav(path: Path, x: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


# ── 響度與延遲 ───────────────────────────────────────────────

def lufs(path: Path) -> float:
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128",
                          "-f", "null", "-"], capture_output=True, text=True, errors="replace").stderr
    found = re.findall(r"I:\s*(-?\d+\.?\d*)\s*LUFS", err)
    return float(found[-1]) if found else -70.0


def lag(ref: np.ndarray, proc: np.ndarray) -> float:
    """proc 比 ref 晚幾秒。用起音包絡（每 1 ms）互相關，對降噪造成的音色變化不敏感。"""
    def onset(x):
        w = SR // 1000
        e = np.log(np.sqrt((x[: len(x) // w * w].reshape(-1, w) ** 2).mean(axis=1)) + 1e-9)
        d = np.clip(np.diff(e), 0, None)
        return (d - d.mean()) / (d.std() + 1e-9)
    # 取能量起伏最大（最多話）的 30 秒來量
    seg, span = SR * 30, 300
    starts = range(0, max(1, len(ref) - seg), SR * 10)
    s0 = max(starts, key=lambda s: float(np.std(ref[s:s + seg])))
    a, b = onset(ref[s0:s0 + seg]), onset(proc[s0:s0 + seg])
    n = min(len(a), len(b)) - 2 * span
    if n <= 0:
        return 0.0
    scores = [float(np.dot(a[span:span + n], b[span + k:span + k + n])) / n for k in range(-span, span + 1)]
    k = int(np.argmax(scores)) - span
    return k / 1000 if max(scores) > 0.2 else 0.0


# ── 主流程 ───────────────────────────────────────────────────

def source_fp(path: str) -> str:
    st = Path(path).stat()
    return f"{st.st_size}-{int(st.st_mtime)}"


def prepare(job: Path, sids=None, method: str = "auto", force: bool = False) -> list:
    """替 sids（預設：有逐字稿的素材）準備好 prepared/<ID>.wav，已是最新的就略過。回傳處理過的代號。"""
    sources = {s["id"]: s for s in read_json(job / "sources.json")["sources"]}
    spoken = None
    if (job / "units.json").exists():
        spoken = {u["source"] for u in read_json(job / "units.json")["units"]}
    if sids is None:
        sids = [sid for sid, s in sources.items() if "asr_audio" in s and (spoken is None or sid in spoken)]
    out_dir = job / "prepared"
    out_dir.mkdir(exist_ok=True)
    done = []
    for sid in sids:
        src = sources[sid]
        if "asr_audio" not in src:
            continue
        wav, meta = out_dir / f"{sid}.wav", out_dir / f"{sid}.json"
        want = {"v": VERSION, "source": source_fp(src["path"]), "target": TARGET}
        if not force and wav.exists() and meta.exists():
            old = read_json(meta)
            # auto（render 自動呼叫）只補缺的或過期的，不會蓋掉先前手動指定的降噪方式
            if all(old.get(k) == v for k, v in want.items()) and method in ("auto", old.get("request")):
                continue
        want["request"] = method
        q = quality(job / src["asr_audio"])
        use = method
        if method == "auto":
            use = "dfn" if q["snr"] < 20 and dfn_available() else "afftdn"
        if use == "dfn" and not dfn_available():
            sys.exit("沒有安裝 DeepFilterNet：pip install deepfilternet torchaudio")
        ref = decode(src["path"], "highpass=f=70")
        if use == "afftdn":
            nr = 6 if q["snr"] >= 30 else 10 if q["snr"] >= 20 else 14
            y = decode(src["path"], f"highpass=f=70,afftdn=nr={nr}:tn=1")
        elif use == "dfn":
            y = dfn_denoise(ref)
        else:
            y = ref.copy()
        d = lag(ref, y)
        if d > 0:
            y = np.concatenate([y[int(d * SR):], np.zeros(int(d * SR), dtype=np.float32)])
        elif d < 0:
            y = np.concatenate([np.zeros(int(-d * SR), dtype=np.float32), y[: len(y) - int(-d * SR)]])
        write_wav(wav, y)
        gain = TARGET - lufs(wav)
        write_wav(wav, y * 10 ** (gain / 20))
        write_json(meta, {**want, "denoise": use, "snr": round(q["snr"], 1), "drift": round(q["drift"], 1),
                          "clip": round(q["clip"], 3), "gain_db": round(gain, 1), "lag_ms": round(d * 1000, 1)})
        warn = "  ⚠ 削波嚴重，降噪救不回來" if q["clip"] > 0.5 else ""
        print(f"{sid}  訊噪比 {q['snr']:5.1f} dB  噪音起伏 {q['drift']:4.1f} dB  降噪 {use:6}  "
              f"增益 {gain:+5.1f} dB  延遲補償 {d * 1000:+5.1f} ms{warn}", flush=True)
        done.append(sid)
    return done


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--sources", nargs="*", help="只處理這些素材（預設：有逐字稿的全部）")
    parser.add_argument("--denoise", choices=["auto", "afftdn", "dfn", "none"], default="auto")
    parser.add_argument("--force", action="store_true", help="已經是最新的也重做")
    args = parser.parse_args()
    done = prepare(args.job, args.sources or None, args.denoise, args.force)
    if not done:
        print("都已是最新，沒有要處理的素材（要重做請加 --force）")


if __name__ == "__main__":
    sys.exit(main())
