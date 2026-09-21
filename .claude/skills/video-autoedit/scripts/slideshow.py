"""把一批靜態圖片（照片、投影片）做成一支空鏡素材：每張固定秒數，輪流緩慢推近／拉遠。

用法（在專案根目錄執行）:
    python slideshow.py samples/投影片/ --out jobs/<job>/img/slides.mp4 [--seconds 8] [--size 1920x1080]
    python probe.py 訪談.mp4 jobs/<job>/img/slides.mp4 --job jobs/<job>     # 再登記成空鏡素材

會印出每張圖在影片裡的起訖秒數（也寫成 slides.json）。EDL 的 broll `in` 從這張表挑，
而且一段空鏡要整段落在同一張圖內——講者說「這一張」時畫面剛好換下一張，觀眾會搞混。

--mode whole：整張圖從頭到尾都在畫面內（縮到推到最近時剛好填滿，四周補模糊背景）。投影片用這個，
  推鏡才不會切到貼邊的字。
--mode crop：裁成滿版，推鏡會再切掉邊緣幾 %。照片用這個最有電影感。
--mode auto（預設）：比例和畫面接近的用 crop，差很多的（直式照片、4:3 的圖）用 whole。

推鏡用 zoompan，但 zoompan 的裁切框是整數像素，慢速推鏡會「卡幾格再跳一格」看起來在抖；
先把圖放大到輸出的 3 倍再推，一格的誤差只剩三分之一像素，就順了。
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def natural(path: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


def image_size(path: Path) -> tuple:
    """轉正後的寬高。ffmpeg 會依照片的 EXIF 方向自動轉正，所以要量解碼後的，不能看檔頭。"""
    ppm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf", "scale=400:-1",
                          "-f", "image2pipe", "-vcodec", "ppm", "-"], capture_output=True, check=True).stdout
    _, w, h, _ = ppm.split(maxsplit=3)
    return int(w), int(h)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("images", nargs="+", type=Path, help="圖片檔或資料夾（依檔名的自然順序）")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=8.0, help="每張停留秒數")
    parser.add_argument("--size", default="1920x1080")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--zoom", type=float, default=0.085, help="推近／拉遠的幅度（0.085 = 8.5%%）")
    parser.add_argument("--mode", choices=["auto", "whole", "crop"], default="auto",
                        help="whole：整張圖始終在畫面內（投影片）；crop：裁成滿版（照片）；auto：依比例決定")
    args = parser.parse_args()

    files = []
    for p in args.images:
        files += sorted((f for f in p.iterdir() if f.suffix.lower() in EXTS), key=natural) if p.is_dir() else [p]
    if not files:
        sys.exit("沒有找到圖片")
    w, h = (int(v) for v in args.size.split("x"))
    W, H = w * 3, h * 3
    n = round(args.seconds * args.fps)
    work = args.out.parent / f"{args.out.stem}_parts"
    work.mkdir(parents=True, exist_ok=True)

    table, lines = [], []
    for i, f in enumerate(files):
        iw, ih = image_size(f)
        # 裁成滿版會切掉的比例：16:10 約 10%，3:2 約 16%，4:3 為 25%，直式照片一大半
        loss = 1 - min(iw * h, ih * w) / max(iw * h, ih * w)
        whole = args.mode == "whole" or (args.mode == "auto" and loss > 0.11)
        if whole:
            # 推到最近時圖剛好填滿畫面，所以推鏡全程都看得到整張
            fw, fh = int(W / (1 + args.zoom)), int(H / (1 + args.zoom))
            frame = (f"split[a][b];[a]scale={w // 4}:{h // 4}:force_original_aspect_ratio=increase,"
                     f"crop={w // 4}:{h // 4},gblur=sigma=8,eq=brightness=-0.1,scale={W}:{H}[bg];"
                     f"[b]scale={fw}:{fh}:force_original_aspect_ratio=decrease:force_divisible_by=2:"
                     f"flags=lanczos[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
        else:
            frame = f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H}"
        part = work / f"{i + 1:03d}.mp4"
        z = f"1+{args.zoom}*on/{n - 1}" if i % 2 == 0 else f"{1 + args.zoom}-{args.zoom}*on/{n - 1}"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(f),
            "-vf", (f"{frame},zoompan=z='{z}':d={n}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                    f"s={w}x{h}:fps={args.fps},setsar=1,format=yuv420p"),
            "-frames:v", str(n), "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
            "-video_track_timescale", str(args.fps * 1000), str(part)], check=True)
        start = i * n / args.fps
        table.append({"index": i + 1, "file": f.name, "start": round(start, 3), "end": round(start + n / args.fps, 3)})
        lines.append(f"file '{part.resolve().as_posix()}'\nduration {n / args.fps:.6f}\n")
        print(f"{i + 1:3d}  {start:7.1f}～{start + n / args.fps:7.1f}s  {f.name}{'  （整張放入）' if whole else ''}",
              flush=True)

    lst = work / "list.txt"
    lst.write_text("".join(lines), encoding="utf-8")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", str(args.out)], check=True)
    args.out.with_suffix(".json").write_text(json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")
    for part in work.iterdir():
        part.unlink()
    work.rmdir()
    print(f"→ {args.out}（{len(files)} 張，{len(files) * n / args.fps:.0f} 秒）")


if __name__ == "__main__":
    sys.exit(main())
