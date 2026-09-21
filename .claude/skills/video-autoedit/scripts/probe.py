"""建立 job：登記素材、檢查規格、抽出辨識用音訊。

用法（在專案根目錄執行）:
    python probe.py samples/a.mp4 samples/b.mov --job jobs/<job>
    python probe.py "samples/活動全程.mp4#00:12:00-00:18:30" --job jobs/<job>   # 長錄影只取一段

產生:
    <job>/sources.json      素材代號（A001…）、路徑與規格
    <job>/audio/<ID>.wav    16 kHz 單聲道，給語音辨識用
    <job>/media/…           以 #起-迄 指定的片段（精確定位＋重新編碼，影音兩軌從同一瞬間開始）

長錄影只取要用的段落：辨識時間與硬碟都省很多。不要用 -c copy 切，它只能從關鍵影格開始，
影像會比聲音早開始零點幾秒。

同一個代號換了素材時，舊的辨識音訊、逐字稿與整理過的音軌會一併刪掉，避免沿用到錯的內容。
"""
import argparse
import json
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

SEGMENT = re.compile(r"^(.*)#(\d[\d:.]*)-(\d[\d:.]*)$")


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


def seconds(text: str) -> float:
    t = 0.0
    for part in text.split(":"):
        t = t * 60 + float(part)
    return t


def cut_segment(path: Path, a: float, b: float, job: Path) -> Path:
    """長錄影只取 a～b 秒，存到 <job>/media/。已經切過就直接用。"""
    tag = "-".join(f"{int(t // 3600):02d}{int(t % 3600 // 60):02d}{t % 60:04.1f}".replace(".", "_") for t in (a, b))
    out = job / "media" / f"{path.stem}_{tag}.mp4"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from render import gpu_encoder
    venc = gpu_encoder("h264", 16) or ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16"]
    tmp = out.with_suffix(".tmp.mp4")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-accurate_seek",
                    "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
                    *venc, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(tmp)],
                   check=True)
    tmp.replace(out)
    return out


def describe(path: Path, sid: str) -> dict:
    info = ffprobe(path)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    st = path.stat()
    src = {"id": sid, "path": str(path.resolve()), "fp": f"{st.st_size}-{int(st.st_mtime)}",
           "duration": float(info["format"]["duration"]), "warnings": []}
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
    parser.add_argument("videos", nargs="+", help="影片路徑；長錄影可寫成「路徑#起-迄」只取一段")
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()

    args.job.mkdir(parents=True, exist_ok=True)
    old = {}
    if (args.job / "sources.json").exists():
        old = {s["id"]: s for s in json.loads((args.job / "sources.json").read_text(encoding="utf-8"))["sources"]}
    sources = []
    for i, spec in enumerate(args.videos, 1):
        sid = f"A{i:03d}"
        m = SEGMENT.match(spec)
        path = Path(spec)
        if m and not path.exists():
            path = cut_segment(Path(m.group(1)), seconds(m.group(2)), seconds(m.group(3)), args.job)
        src = describe(path, sid)
        prev = old.get(sid)
        # 舊版 sources.json 沒有 fp，只能比路徑
        if prev and (prev["path"] != src["path"] or prev.get("fp", src["fp"]) != src["fp"]):
            stale = [args.job / "audio" / f"{sid}.wav", *args.job.glob(f"words/{sid}.*"),
                     *args.job.glob(f"prepared/{sid}.*")]
            for f in stale:
                if f.exists():
                    f.unlink()
            print(f"{sid} 換了素材，已刪除舊的辨識音訊、逐字稿與整理過的音軌，請重新辨識")
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
