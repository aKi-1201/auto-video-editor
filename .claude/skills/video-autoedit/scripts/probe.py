"""建立 job：登記素材、檢查規格、抽出辨識用音訊。

用法（在專案根目錄執行）:
    python probe.py samples/a.mp4 samples/b.mov --job jobs/<job>

產生:
    <job>/sources.json      素材代號（A001…）、路徑與規格
    <job>/audio/<ID>.wav    16 kHz 單聲道，給語音辨識用
"""
import argparse
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path


def ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, check=True,
    ).stdout
    return json.loads(out)


def rotation(stream: dict) -> int:
    for sd in stream.get("side_data_list", []):
        if "rotation" in sd:
            return int(sd["rotation"])
    return int(stream.get("tags", {}).get("rotate", 0))


def describe(path: Path, sid: str) -> dict:
    info = ffprobe(path)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    src = {"id": sid, "path": str(path.resolve()), "duration": float(info["format"]["duration"]), "warnings": []}
    if video:
        r = Fraction(video["r_frame_rate"])
        avg = Fraction(video["avg_frame_rate"]) if video["avg_frame_rate"] != "0/0" else r
        src["video"] = {
            "width": video["width"], "height": video["height"],
            "fps": str(avg), "fps_float": round(float(avg), 3), "rotation": rotation(video),
        }
        # r_frame_rate 與 avg_frame_rate 不同通常代表可變幀率（手機常見）
        if r and abs(float(r) - float(avg)) / float(r) > 0.01:
            src["warnings"].append("疑似可變幀率（VFR），渲染時會先轉成固定幀率")
    else:
        src["warnings"].append("沒有影像軌")
    if audio:
        src["audio"] = {"sample_rate": int(audio["sample_rate"]), "channels": audio["channels"]}
    else:
        src["warnings"].append("沒有音軌，無法辨識")
    return src


def extract_audio(path: Path, wav: Path) -> None:
    wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("videos", nargs="+", type=Path)
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()

    args.job.mkdir(parents=True, exist_ok=True)
    sources = []
    for i, path in enumerate(args.videos, 1):
        sid = f"A{i:03d}"
        src = describe(path, sid)
        if "audio" in src:
            wav = args.job / "audio" / f"{sid}.wav"
            if not wav.exists():
                extract_audio(path, wav)
            src["asr_audio"] = str(wav.relative_to(args.job))
        sources.append(src)
        v = src.get("video", {})
        print(f"{sid}  {path.name}  {src['duration'] / 60:.1f} 分  "
              f"{v.get('width')}x{v.get('height')} {v.get('fps_float')}fps  {'；'.join(src['warnings'])}")

    (args.job / "sources.json").write_text(json.dumps({"sources": sources}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
