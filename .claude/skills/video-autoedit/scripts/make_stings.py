"""合成片頭、章節、片尾用的短音效（撥弦音色、五聲音階），不需下載任何素材。

用法（在專案根目錄執行）:
    python make_stings.py            # 產生到 skill 的 assets/audio/

用 Karplus-Strong 撥弦合成，加上簡單殘響。這是暫代用的配樂；
正式影片建議換成授權清楚的音樂（放在 <job>/audio/ 並在 EDL 字卡的 music 欄位指定）。
"""
import sys
import wave
from pathlib import Path

import numpy as np

SR = 48000
OUT = Path(__file__).resolve().parent.parent / "assets" / "audio"
# D 宮五聲音階（D E F# A B），單位 Hz
NOTE = {"D4": 293.66, "E4": 329.63, "F#4": 369.99, "A4": 440.0, "B4": 493.88,
        "D5": 587.33, "E5": 659.25, "F#5": 739.99, "A5": 880.0, "B5": 987.77, "D6": 1174.66,
        "A3": 220.0, "D3": 146.83}


def pluck(freq: float, dur: float, bright: float = 0.6, seed: int = 0) -> np.ndarray:
    """Karplus-Strong 撥弦。bright 越大越亮、衰減越慢。"""
    rng = np.random.default_rng(seed)
    n = int(SR * dur)
    period = SR / freq
    p = int(period)
    frac = period - p
    buf = rng.uniform(-1, 1, p + 1)
    # 撥弦位置濾波：讓起音柔一點
    buf = np.convolve(buf, [0.5, 0.5], mode="same")
    out = np.empty(n)
    decay = 0.996 + 0.0035 * bright
    idx = 0
    for i in range(n):
        a = buf[idx % (p + 1)]
        b = buf[(idx + 1) % (p + 1)]
        out[i] = a
        # 分數延遲 + 低通 + 衰減
        buf[idx % (p + 1)] = decay * ((1 - frac) * 0.5 * (a + b) + frac * b)
        idx += 1
    env = np.minimum(1, np.arange(n) / (0.004 * SR))  # 4 ms 起音，避免爆音
    return out * env


def reverb(x: np.ndarray, seconds: float = 0.9, wet: float = 0.22, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(SR * seconds)
    ir = rng.standard_normal(n) * np.exp(-np.linspace(0, 7, n))
    ir[: int(0.012 * SR)] = 0  # 預延遲
    ir /= np.sqrt((ir ** 2).sum())
    size = len(x) + n
    tail = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)  # FFT 卷積
    dry = np.concatenate([x, np.zeros(n)])
    return (1 - wet) * dry + wet * tail


def phrase(events: list, total: float) -> np.ndarray:
    """events: [(秒, 音名, 長度, 音量, 聲像 -1~1)] → 立體聲。"""
    left = np.zeros(int(SR * total))
    right = np.zeros_like(left)
    for k, (t, name, dur, gain, pan) in enumerate(events):
        s = pluck(NOTE[name], dur, seed=k) * gain
        i = int(t * SR)
        seg = slice(i, min(i + len(s), len(left)))
        s = s[: seg.stop - seg.start]
        left[seg] += s * np.sqrt((1 - pan) / 2)
        right[seg] += s * np.sqrt((1 + pan) / 2)
    stereo = np.stack([reverb(left), reverb(right, seed=2)], axis=1)
    fade = int(0.6 * SR)
    stereo[-fade:] *= np.linspace(1, 0, fade)[:, None]
    return stereo / np.abs(stereo).max() * 0.5  # 峰值 -6 dBFS，實際音量在混音時再調


def save(name: str, data: np.ndarray) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(data, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(OUT / f"{name}.wav"), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print(f"{name}.wav  {len(data) / SR:.1f} 秒")


def main() -> None:
    # 片頭：第一拍就是重音（同時撥三弦），再一個上行小尾巴，整體收得快
    save("sting_title", phrase([
        (0.00, "D3", 2.6, 0.55, 0.0), (0.00, "A3", 2.4, 0.5, -0.35), (0.00, "D4", 2.4, 0.6, 0.3),
        (0.18, "A4", 2.0, 0.4, 0.15), (0.30, "D5", 2.2, 0.45, -0.15),
    ], 3.0))
    # 章節：一個乾淨的重音
    save("sting_chapter", phrase([
        (0.00, "A3", 1.6, 0.4, -0.15), (0.00, "D5", 1.8, 0.5, 0.15),
    ], 2.0))
    # 片尾：下行收束
    save("sting_end", phrase([
        (0.00, "D5", 2.2, 0.5, 0.3), (0.26, "B4", 2.2, 0.5, 0.15), (0.52, "A4", 2.2, 0.5, 0.0),
        (0.78, "F#4", 2.4, 0.45, -0.15), (1.04, "E4", 2.4, 0.45, -0.3),
        (1.30, "D4", 3.0, 0.6, -0.1), (1.32, "A3", 3.0, 0.35, 0.2), (1.34, "D3", 3.0, 0.3, 0.0),
    ], 4.6))


if __name__ == "__main__":
    sys.exit(main())
