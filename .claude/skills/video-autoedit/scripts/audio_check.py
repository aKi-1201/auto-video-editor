"""檢查每支素材的收音品質，判斷要不要額外降噪。

用法（在專案根目錄執行）:
    python audio_check.py --job jobs/<job>

讀 probe.py 產生的 16 kHz 辨識音訊，量四件事：

    訊噪比   說話時的音量 − 背景噪音的音量。最重要的一項。
    噪音起伏 背景噪音在整支片裡的變化量。起伏大 = 冷氣、車聲、機具這類非穩態噪音，
             afftdn 這種頻譜相減的方法處理不好，要換 DeepFilterNet3。
    低頻能量 100 Hz 以下的佔比，高的話是手持震動、桌面碰撞或冷氣的隆隆聲。
    削波     音量頂到 0 dBFS 的比例，高的話是錄音增益開太大，降噪救不回來。

門檻：安靜室內的口播約 30 dB 以上，吵雜環境的訪談常在 20 dB 以下。
降噪只套在成片上，不要套在語音辨識上（見 denoise.py）。
"""
import argparse
import sys
import wave
from pathlib import Path

import numpy as np

from common import read_json


def measure(wav: Path) -> dict:
    with wave.open(str(wav)) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    n = int(sr * 0.02)
    frames = x[: len(x) // n * n].reshape(-1, n)
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)
    floor, speech = np.percentile(db, 10), np.percentile(db, 95)

    # 噪音起伏：切成 10 秒一段，比較各段噪音底的差異
    per = max(1, int(10 / 0.02))
    floors = [np.percentile(db[i:i + per], 10) for i in range(0, max(1, len(db) - per), per)] or [floor]
    drift = float(np.percentile(floors, 90) - np.percentile(floors, 10))

    # 低頻佔比：取最安靜的那些畫格算頻譜
    quiet = frames[db < np.percentile(db, 20)]
    if len(quiet) > 4:
        spec = (np.abs(np.fft.rfft(quiet * np.hanning(n), axis=1)) ** 2).mean(axis=0)
        freq = np.fft.rfftfreq(n, 1 / sr)
        low = float(spec[freq < 100].sum() / (spec.sum() + 1e-12))
    else:
        low = 0.0
    return {"snr": float(speech - floor), "floor": float(floor), "drift": drift,
            "low": low * 100, "clip": float((np.abs(x) > 0.995).mean()) * 100,
            "seconds": len(x) / sr}


def verdict(m: dict, has_speech: bool) -> tuple:
    """回傳 (評語, 建議, 要不要降噪)。

    沒有對白的素材（空鏡）不判斷：它量到的「說話音量」其實只是比較大聲的噪音。
    """
    if not has_speech:
        return "空鏡（無對白）", "聲音不會用到，不用處理", False
    if m["clip"] > 0.5:
        return "削波嚴重", "錄音增益開太大，降噪救不回來", False
    snr, drift = m["snr"], m["drift"]
    if snr >= 30:
        return "乾淨", "不用降噪", False
    if snr >= 20:
        return "普通", "成片用預設的 afftdn 就夠", False
    if drift >= 4:
        return "吵，噪音還會變", "建議降噪：非穩態噪音正是 DFN3 的強項", True
    return "吵，但噪音穩定", "建議降噪，做完聽過再決定要不要用", True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()

    sources = read_json(args.job / "sources.json")["sources"]
    spoken = set()
    if (args.job / "units.json").exists():          # 有逐字稿的才是有對白的素材
        spoken = {u["source"] for u in read_json(args.job / "units.json")["units"]}

    rows = []
    for s in sources:
        if "asr_audio" not in s:
            continue
        m = measure(args.job / s["asr_audio"])
        rows.append((s["id"], m) + verdict(m, not spoken or s["id"] in spoken))
    if not rows:
        sys.exit("沒有可檢查的音訊，請先跑 probe.py")

    head = ("素材", "長度", "訊噪比", "噪音底", "噪音起伏", "低頻", "削波", "評語")
    print("{:6} {:>7} {:>8} {:>8} {:>9} {:>6} {:>6}  {}".format(*head))
    for sid, m, note, _, _ in rows:
        snr = "—" if note.startswith("空鏡") else "{:.1f}dB".format(m["snr"])
        print("{:6} {:6.1f}分 {:>8} {:7.1f}dB {:8.1f}dB {:5.1f}% {:5.2f}%  {}".format(
            sid, m["seconds"] / 60, snr, m["floor"], m["drift"], m["low"], m["clip"], note))

    print()
    for sid, _, _, tip, _ in rows:
        print(f"  {sid}：{tip}")
    need = [sid for sid, _, _, _, flag in rows if flag]
    if need:
        print(f"\n跑降噪：python S/denoise.py --job {args.job.as_posix()} --sources {' '.join(need)}")
    else:
        print("\n收音都可以，不需要額外降噪。")


if __name__ == "__main__":
    sys.exit(main())
