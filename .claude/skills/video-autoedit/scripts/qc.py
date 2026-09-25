"""成片品質檢查：渲染完跑一次，⚠ 的項目處理完再回報使用者。

用法（在專案根目錄執行）:
    python qc.py --job jobs/<job>

檢查規格、時間戳連續性、影音同步（硬切點有沒有隨片長漂移）、響度、配樂音量、字幕、鏡頭長度、
切點（有沒有切到字尾、刪停頓有沒有刪到聲音），並把每個疊加字卡、全畫面字卡、推鏡片段與幾個取樣點
的實際畫面拼成 <job>/qc/overview.png——人名條是不是對的人、字幕與面板有沒有擋到臉、推鏡有沒有裁到人或字，一定要打開看。
"""
import argparse
import json
import math
import re
import subprocess
import sys
import wave
from fractions import Fraction
from pathlib import Path

import numpy as np

from common import load_units, read_json

OK, WARN = "✓", "⚠"


def probe_packets(path: Path, stream: str) -> list:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries",
                          "packet=pts_time,duration_time", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout
    rows = []
    for line in out.split():
        parts = line.split(",")
        if len(parts) >= 2 and parts[0] not in ("", "N/A") and parts[1] not in ("", "N/A"):
            rows.append((float(parts[0]), float(parts[1])))
    return sorted(rows)


def pcm(path: Path, rate: int = 8000, af: str = "") -> np.ndarray:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn"]
    if af:
        cmd += ["-af", af]
    raw = subprocess.run(cmd + ["-ac", "1", "-ar", str(rate), "-f", "f32le", "-"], capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def db_frames(x: np.ndarray, rate: int, hop: float) -> np.ndarray:
    n = int(rate * hop)
    return 20 * np.log10(np.sqrt((x[: len(x) // n * n].reshape(-1, n) ** 2).mean(axis=1)) + 1e-9)


def power(db: np.ndarray) -> float:
    """一串 dB 值的能量平均。"""
    return float(10 * np.log10(np.mean(10 ** (db / 10))))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--file", default="final.mp4")
    args = parser.parse_args()
    job, final = args.job, args.job / args.file
    tl = read_json(job / "timeline.json")
    fps = Fraction(tl["output"]["fps"])
    pieces = tl["pieces"]
    warns = 0

    def report(ok: bool, text: str):
        nonlocal warns
        warns += not ok
        print(f"{OK if ok else WARN} {text}")

    # ── 規格與時間戳 ──
    info = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                           "stream=codec_type,codec_name,width,height,start_time", "-show_entries",
                           "format=duration", "-of", "json", str(final)], capture_output=True, text=True).stdout
    meta = json.loads(info)
    v = next(s for s in meta["streams"] if s["codec_type"] == "video")
    starts = [float(s.get("start_time", 0)) for s in meta["streams"]]
    report(all(abs(s) < 1e-3 for s in starts),
           f"規格 {v['codec_name']} {v['width']}x{v['height']}，{float(meta['format']['duration']):.2f} 秒，"
           f"各軌起點 {starts}")
    ends = {}
    for st, name in (("v", "影像"), ("a", "音訊")):
        pk = probe_packets(final, st)
        gaps = sum(1 for (p0, d0), (p1, _) in zip(pk, pk[1:]) if abs(p0 + d0 - p1) > 1e-4)
        ends[st] = pk[-1][0] + pk[-1][1]
        report(gaps == 0, f"{name}時間戳不連續 {gaps} 處（有缺口時播放會突然快轉）")
    report(abs(ends["v"] - ends["a"]) < 0.01, f"影音長度差 {abs(ends['v'] - ends['a']) * 1000:.0f} ms")

    # ── 影音同步：換素材的硬切點，實際畫面切換要落在排定的那一格，而且不能隨片長累積 ──
    W, H = 64, 36
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(final), "-vf", f"scale={W}:{H},format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    fr = np.frombuffer(raw[: len(raw) // (W * H) * W * H], np.uint8).astype(np.float32).reshape(-1, W * H)
    diff = np.abs(np.diff(fr, axis=0)).mean(axis=1)
    thr = diff.mean() + 4 * diff.std()
    peaks = [(i + 1) / float(fps) for i in range(len(diff)) if diff[i] > thr and diff[i] == diff[max(0, i - 3):i + 4].max()]
    cuts = [q["out_start"] for p, q in zip(pieces, pieces[1:]) if p["type"] == "clip" and q["type"] == "clip"
            and p["source"] != q["source"] and "broll" not in p and "broll" not in q]
    if cuts and peaks:
        dev = [min(abs(c - t) for t in peaks) * 1000 for c in cuts]
        report(max(dev) <= 1000 / float(fps) + 1,
               f"換素材的硬切 {len(cuts)} 處，實際切換與排定時間最大差 {max(dev):.0f} ms（一幀 {1000 / float(fps):.0f} ms）")

    # ── 響度 ──
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(final), "-af", "ebur128=peak=true",
                          "-f", "null", "-"], capture_output=True, text=True, errors="replace").stderr
    lu = float(re.findall(r"I:\s*(-?\d+\.?\d*)\s*LUFS", err)[-1])
    peak = float(re.findall(r"Peak:\s*(-?\d+\.?\d*)\s*dBFS", err)[-1])
    target = tl["output"].get("loudness_lufs", -14)
    report(abs(lu - target) <= 1 and peak <= -0.5, f"響度 {lu:.1f} LUFS（目標 {target}），峰值 {peak:.1f} dBFS")

    # ── 配樂：鋪底配樂在講話時比人聲低多少、字卡配樂比對白低多少 ──
    # 0.4 秒區塊的能量平均，再扣掉比平均低 10 dB 以上的區塊（停頓），和 LUFS 的算法同一個道理
    levels = job / "audio_levels.json"
    if levels.exists() and (job / "music_bed.wav").exists() and read_json(levels).get("music_eq"):
        lv, rate, hop = read_json(levels), 8000, 0.4
        bed = db_frames(pcm(job / "music_bed.wav", rate, lv["music_eq"]), rate, hop) + lv["final_gain_db"]
        mix = db_frames(pcm(final, rate), rate, hop)
        n = min(len(bed), len(mix))
        bed, mix = bed[:n], mix[:n]
        clip, card = np.zeros(n, bool), np.zeros(n, bool)
        for p in pieces:
            a, b = int(p["out_start"] / hop), int(p["out_end"] / hop)
            if p["type"] == "clip":
                clip[a:b] = True
            elif p.get("music"):
                card[a + 1:b - 1] = True
        talk = clip & (mix > power(mix[clip]) - 10)
        under = talk & (bed > -60)
        if under.sum() > 10:
            gap = power(mix[under]) - power(bed[under])
            report(12 <= gap <= 20, f"鋪底配樂在講話時比人聲低 {gap:.1f} dB（約 15 最自然）")
        if card.sum() > 3:
            gap = power(mix[talk]) - power(mix[card])
            report(1 <= gap <= 5, f"字卡配樂比對白低 {gap:.1f} dB（2～4 最自然）")

    # ── 字幕 ──
    ass = job / "captions.ass"
    if ass.exists():
        cues = []
        for line in ass.read_text(encoding="utf-8").splitlines():
            if line.startswith("Dialogue:"):
                f = line.split(",", 9)
                t = [sum(float(x) * m for x, m in zip(s.split(":"), (3600, 60, 1))) for s in (f[1], f[2])]
                cues.append((t[0], t[1], re.sub(r"\{.*?\}", "", f[9]).strip()))
        bad = [c for c in cues if c[1] - c[0] < 0.5 or len(c[2].replace("　", "").replace(" ", "")) <= 2
               or c[2].startswith("的")]
        report(not bad, f"字幕 {len(cues)} 條，過短、一兩個字自成一條、以「的」開頭的：{len(bad)} 條")
        for c in bad[:8]:
            print(f"     {c[0]:7.2f}s「{c[2]}」")

    # ── 鏡頭長度（依畫面來源＋推鏡合併）──
    shots = []
    for p in pieces:
        key = ("card", p["item"]) if p["type"] == "card" else (
            p["source"], p.get("zoom"), (p["broll"]["source"], round(p["broll"]["in"] - p["broll"]["played"], 2))
            if "broll" in p else None)
        dur = p["frames"] / float(fps)
        if shots and shots[-1][0] == key:
            shots[-1][2] += dur
        else:
            shots.append([key, p["out_start"], dur])
    short = [s for s in shots if s[2] < 1.2]
    report(not short, f"鏡頭 {len(shots)} 個，最短 {min(s[2] for s in shots):.2f} 秒（不應短於 1.2 秒）")
    for s in short[:5]:
        print(f"     {s[1]:7.2f}s 只有 {s[2]:.2f} 秒")

    # ── 切點：片段結尾有沒有切到最後一個字、刪停頓有沒有刪到聲音 ──
    edl, units = read_json(job / "edl.json"), load_units(job)
    sources = {s["id"]: s for s in read_json(job / "sources.json")["sources"]}
    clipped = []
    for idx, it in enumerate(edl["timeline"]):
        if it["type"] != "clip" or it.get("nudge", {}).get("end"):
            continue
        end = max(p["src_end"] for p in pieces if p["item"] == idx)
        last = units[it["to"]]["words"][-1]["end"]
        if end < last - 0.02:
            clipped.append(f"{it['to']} 結尾早了 {last - end:.2f} 秒")
    report(not clipped, f"片段結尾切到字尾：{len(clipped)} 處" + ("（" + "；".join(clipped[:4]) + "）" if clipped else ""))
    removed = []
    for p, q in zip(pieces, pieces[1:]):
        if p["type"] == "clip" and q["type"] == "clip" and p["item"] == q["item"] and q["src_start"] - p["src_end"] > 0.05:
            with wave.open(str(job / sources[p["source"]]["asr_audio"])) as w:
                sr = w.getframerate()
                w.setpos(int(p["src_end"] * sr))
                x = np.frombuffer(w.readframes(int((q["src_start"] - p["src_end"]) * sr)), np.int16) / 32768
            db = db_frames(x.astype(np.float32), sr, 0.04)
            if len(db) and db.max() - np.percentile(db, 20) > 6:
                removed.append(f"{p['source']} {p['src_end']:.2f}～{q['src_start']:.2f}s")
    report(not removed, f"刪掉的停頓裡有明顯聲音：{len(removed)} 處" + ("（" + "；".join(removed[:4]) + "；"
           "可能刪到很輕的字，該段設 tighten: false）" if removed else ""))

    # ── 畫面：每個疊加字卡、每張全畫面字卡、每個推鏡片段，再平均取幾格 ──
    marks = [((o["start"] + min(1.5, (o["end"] - o["start"]) / 2)), f"疊加 {o['type']} {o['fields'].get('name', '')}")
             for o in tl["overlays"]]
    marks += [((p["out_start"] + p["out_end"]) / 2, f"字卡 item{p['item']}") for p in pieces if p["type"] == "card"]
    # 推鏡會裁掉畫面四周：每個有推鏡的片段至少取一格，才看得出有沒有裁到人或字
    zoomed = {}
    for p in pieces:
        if p["type"] == "clip" and p.get("zoom"):
            zoomed.setdefault(p["item"], []).append(p)
    for ps in zoomed.values():
        if not any(q["out_start"] <= t <= q["out_end"] for q in ps for t, _ in marks):
            q = max(ps, key=lambda q: q["out_end"] - q["out_start"])
            marks.append(((q["out_start"] + q["out_end"]) / 2, f"推鏡 {q['source']}"))
    total = tl["duration"]
    marks += [(t, "取樣") for t in (total * (k + 0.5) / 8 for k in range(8))
              if all(abs(t - m) > 3 for m, _ in marks)]
    marks.sort()
    frames = [min(round(t * fps), len(fr) - 1) for t, _ in marks]
    out_dir = job / "qc"
    out_dir.mkdir(exist_ok=True)
    cols = 4
    rows = math.ceil(len(frames) / cols)
    sel = "+".join(f"eq(n\\,{n})" for n in frames)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(final), "-vf",
                    f"select='{sel}',scale=480:-2,tile={cols}x{rows}", "-fps_mode", "passthrough",
                    "-frames:v", "1", str(out_dir / "overview.png")], check=True)
    print(f"\n畫面總覽 → {out_dir / 'overview.png'}（由左到右、由上到下）：")
    for k, (t, label) in enumerate(marks):
        print(f"  {k + 1:2d}. {int(t // 60):02d}:{t % 60:05.2f}  {label}")
    print(f"\n{'全部通過' if not warns else f'{warns} 項要處理'}；總覽圖一定要打開看。")


if __name__ == "__main__":
    sys.exit(main())
