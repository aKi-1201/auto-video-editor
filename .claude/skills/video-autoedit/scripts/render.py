"""依 edl.json 渲染影片。

用法（在專案根目錄執行）:
    python render.py --job jobs/<job> --plan-only   # 只算時間軸 timeline.json（captions.py 需要）
    python render.py --job jobs/<job> --preview     # 960x540 快速預覽 preview.mp4
    python render.py --job jobs/<job>               # 成片 final.mp4

步驟:
    0. audio   各素材降噪、音量對齊（prep_audio.py；缺的或過期的會自動補做）
    1. plan    句子 ID → 素材秒數；切點對齊靜音；刪掉過長停頓；決定跳剪時的推鏡
    2. pieces  每一小段各自重新編碼成中繼檔（固定幀率、統一格式，可快取重用）
    3. concat  串接成 body.mov
    4. finish  燒入字幕、混配樂、響度標準化

先跑過 cards.py（字卡 PNG）與 captions.py（字幕），finish 才會帶上它們。
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import wave
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

import numpy as np

from common import fmt_ts, load_sources, load_units, read_json, write_json

DEFAULT_OUTPUT = {
    # 預設升採樣到 1440p：升採樣不會增加細節，但 YouTube 對 1440p 以上的上傳
    # 會用 VP9／AV1 並分到較高位元率，觀眾看到的壓縮痕跡比較少。代價是檔案約 1.7 倍。
    # 編碼用 H.264：到哪都能播（HEVC 在 Windows 要另裝擴充功能）；YouTube 反正會重新編碼。
    "width": 2560, "height": 1440, "codec": "h264", "fps": "30000/1001", "loudness_lufs": -14,
    # 同一支素材跳剪時交替放大，遮掩畫面跳動；center 是放大中心（0～1，依人物位置調整）
    "punch_in": {"zoom": 1.12, "center": [0.5, 0.45]},
    # 超過 max 秒的停頓縮成約 keep 秒。keep_after 要夠長：靜音偵測常把字尾的尾音
    # 當成已經沒聲音，切太前面會把最後一個字吃掉，講話就變得不流暢
    "pause": {"max": 1.2, "keep_after": 0.45, "keep_before": 0.25},
}
PAD_IN, PAD_OUT = 0.12, 0.30      # 句首、句尾預留（句尾留多一點，避免吃掉尾音）
MIN_SHOT = 0.8                     # 最短鏡頭：比這短的碎片會看起來像畫面在亂閃
DISSOLVE = 0.35                    # 空鏡進出的溶接秒數
BIG_JUMP = 1.5                     # 刪掉超過這麼多秒才換鏡位
SR = 48000


class Silence:
    """以 16 kHz 辨識音訊的音量判斷靜音，給切點對齊與刪停頓用。"""

    def __init__(self, wav: Path, hop: float = 0.02):
        with wave.open(str(wav)) as w:
            sr = w.getframerate()
            data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        n = int(sr * hop)
        frames = data[: len(data) // n * n].reshape(-1, n)
        db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)
        floor, loud = np.percentile(db, 10), np.percentile(db, 95)
        # 門檻放低一點：寧可少判成靜音，也不要把字尾的尾音當成靜音而切掉
        self.threshold = floor + max(4.0, 0.22 * (loud - floor))
        silent = db < self.threshold
        # 靜音中短於 80 ms 的雜音（咔噠聲）視為靜音
        runs = self._runs(~silent)
        for a, b in runs:
            if b - a < 4 and a > 0 and b < len(silent):
                silent[a:b] = True
        self.silent, self.hop = silent, hop

    @staticmethod
    def _runs(mask):
        idx = np.flatnonzero(np.diff(np.concatenate([[0], mask.astype(np.int8), [0]])))
        return list(zip(idx[::2], idx[1::2]))

    def at(self, t: float) -> bool:
        i = int(t / self.hop)
        return bool(self.silent[i]) if 0 <= i < len(self.silent) else True

    def gaps(self, a: float, b: float, min_len: float) -> list:
        i0, i1 = int(a / self.hop), int(b / self.hop)
        out = []
        for s, e in self._runs(self.silent[i0:i1]):
            if (e - s) * self.hop >= min_len:
                out.append(((i0 + s) * self.hop, (i0 + e) * self.hop))
        return out


def refine_start(t: float, sil: Silence, floor: float) -> float:
    t0 = max(t - PAD_IN, floor)
    for k in range(19):  # 往前最多 0.36 秒找靜音，避免切掉字頭
        tt = t0 - k * 0.02
        if tt < floor:
            break
        if sil.at(tt):
            return tt
    return t0


def refine_end(t: float, sil: Silence, ceil: float) -> float:
    t1 = min(t + PAD_OUT, ceil)
    for k in range(60):  # 往後最多 1.2 秒等聲音結束，避免切掉字尾
        tt = t1 + k * 0.02
        if tt > ceil:
            break
        if sil.at(tt):
            return min(tt + 0.12, ceil)
    return t1


def canvas(sources: dict, edl: dict) -> dict:
    """輸出大小：預設是 DEFAULT_OUTPUT 的 1440p，但素材比它大時就跟著素材走。

    只會往上不會往下，避免哪天丟進 4K 素材被默默降級。主鏡頭 = 時間軸第一個 clip 的素材。
    """
    first = next((it["source"] for it in edl["timeline"] if it["type"] == "clip"), None)
    v = (sources.get(first) or {}).get("video")
    if not v:
        return {}
    w, h = v["width"], v["height"]
    if abs(int(v.get("rotation") or 0)) in (90, 270):
        w, h = h, w
    if w * h <= DEFAULT_OUTPUT["width"] * DEFAULT_OUTPUT["height"]:
        return {}
    return {"width": w // 2 * 2, "height": h // 2 * 2}


def plan(job: Path, edl: dict, cards: dict) -> dict:
    sources = load_sources(job)
    out = {**DEFAULT_OUTPUT, **canvas(sources, edl), **edl.get("output", {})}
    fps = Fraction(str(out["fps"]))
    units = load_units(job)
    order = {sid: sorted((u for u in units.values() if u["source"] == sid), key=lambda u: u["start"])
             for sid in sources}
    silences = {sid: Silence(job / src["asr_audio"]) for sid, src in sources.items() if "asr_audio" in src}
    pause = out["pause"]

    pieces, t_out, zoom, prev = [], Fraction(0), 0, None

    def add(piece, dur_sec):
        nonlocal t_out
        frames = max(1, round(dur_sec * fps))
        dur = Fraction(frames) / fps
        piece.update(frames=frames, out_start=float(t_out), out_end=float(t_out + dur))
        if "src_start" in piece:
            piece["src_end"] = piece["src_start"] + float(dur)
        pieces.append(piece)
        t_out += dur

    for idx, item in enumerate(edl["timeline"]):
        if item["type"] == "card":
            add({"type": "card", "item": idx, "png": cards[idx], "fade": item.get("fade", 0.3),
                 "music": item.get("music"), "music_gain": item.get("music_gain", 0)},
                float(item["duration"]))
            zoom, prev = 0, None
            continue

        sid = item["source"]
        seq = order[sid]
        first = next(i for i, u in enumerate(seq) if u["id"] == item["from"])
        last = next(i for i, u in enumerate(seq) if u["id"] == item["to"])
        floor = seq[first - 1]["end"] + 0.02 if first > 0 else 0.0
        ceil = seq[last + 1]["start"] - 0.02 if last + 1 < len(seq) else sources[sid]["duration"]
        sil = silences[sid]
        a = refine_start(seq[first]["start"], sil, floor) + item.get("nudge", {}).get("start", 0)
        b = refine_end(seq[last]["end"], sil, ceil) + item.get("nudge", {}).get("end", 0)

        # 刪長停頓：把區間切成數段
        spans, cur = [], a
        if item.get("tighten", True):
            words = [(w["start"], w["end"]) for u in seq[first:last + 1] for w in u["words"]]
            for g0, g1 in sil.gaps(a, b, pause["max"]):
                cut0, cut1 = protect_words(g0 + pause["keep_after"], g1 - pause["keep_before"], words)
                if cut1 - cut0 > 0.2 and cut0 - cur >= MIN_SHOT:
                    spans.append((cur, cut0))
                    cur = cut1
        spans.append((cur, b))
        if len(spans) > 1 and spans[-1][1] - spans[-1][0] < MIN_SHOT:
            spans[-2:] = [(spans[-2][0], spans[-1][1])]   # 尾巴太短，寧可把這個停頓留著

        # 空鏡：畫面換成別的素材、聲音仍是訪談。在這裡就把區間切開，
        # 直接編進片段裡（不要留到最後合成才疊，十幾路影片掛在疊加鏈上會互相卡住）
        windows = broll_windows(edl, units, sid, item)
        snap_windows(windows, spans, a, b)
        for w in windows:
            spans = [s for span in spans for s in split_span(span, w["start"], w["end"])]
        pngs = overlay_windows(edl, units, sid, item, cards)
        for g in pngs:
            spans = [s for span in spans for s in split_span(span, g["start"], g["end"])]
        spans.sort()

        for s0, s1 in spans:
            if prev is not None:
                if prev["source"] != sid:
                    zoom = 0          # 換素材（換機位）本身就不是跳剪，不用放大去遮
                else:
                    jump = s0 - prev["src_end"]
                    if jump >= BIG_JUMP or (prev["item"] != idx and abs(jump) > 0.05):
                        zoom ^= 1
            # punch_in: false 的片段永遠不推鏡（畫面上有燒死的字幕／logo、人站在邊緣時會被裁掉）
            piece = {"type": "clip", "item": idx, "source": sid, "src_start": s0,
                     "zoom": zoom if item.get("punch_in", True) else 0}
            if (job / "prepared" / f"{sid}.wav").exists():
                piece["audio"] = f"prepared/{sid}.wav"
            w = next((w for w in windows if w["start"] - 0.01 <= s0 < w["end"]), None)
            if w and s1 - s0 >= 0.2:
                # 空鏡素材的起點要扣掉這段之前已經播掉的時間（停頓被刪掉時也算）
                played = sum(max(0.0, min(e, s0) - max(b0, w["start"])) for b0, e in spans if b0 < s0)
                total = sum(min(e, w["end"]) - max(b0, w["start"])
                            for b0, e in spans if b0 < w["end"] and e > w["start"])
                piece["broll"] = {"source": w["source"], "in": round(w["in"] + played, 3),
                                  "played": round(played, 3), "window": round(total, 3)}
                piece["zoom"] = 0
            g = next((g for g in pngs if g["start"] - 0.01 <= s0 < g["end"]), None)
            if g:
                played = sum(max(0.0, min(e, s0) - max(b0, g["start"])) for b0, e in spans if b0 < s0)
                piece["png_overlay"] = {"png": g["png"], "played": round(played, 3),
                                        "window": round(g["end"] - g["start"], 3)}
            add(piece, s1 - s0)
            prev = piece

    # 兩段空鏡首尾相接時，圖和圖之間直接切換：各自溶接的話會先淡回底下的訪談畫面再淡出，
    # 中間閃一下不到一秒的訪談
    for p, q in zip(pieces, pieces[1:]):
        if "broll" in p and "broll" in q and abs(p["out_end"] - q["out_start"]) < 1e-6:
            p["broll"]["cut_out"] = q["broll"]["cut_in"] = True

    # 疊加字卡的時間：at 句開始時出現，不跨進全畫面字卡
    overlays = []
    for i, ov in enumerate(edl.get("overlays", [])):
        u = units[ov["at"]]
        # 先加 offset 再換算成片時間：片段階段也是用「素材時間 at+offset」決定何時燒入疊加圖，
        # 兩邊用同一個基準才不會出現「時間軸說 2:07 出現、畫面上 2:12 才出現」的落差
        t = map_time(pieces, u["source"], u["start"] + float(ov.get("offset", 0)))
        if t is None:
            print(f"警告：疊加字卡 {i} 的 {ov['at']} 不在成片中，略過", file=sys.stderr)
            continue
        end = t + float(ov.get("duration", 5))
        nxt = next((p["out_start"] for p in pieces if p["type"] == "card" and p["out_start"] >= t), None)
        if nxt is not None:
            end = min(end, nxt - 0.05 if ov["type"] == "broll" else nxt - 0.2)
        if ov["type"] == "broll":
            continue  # 已經在片段階段處理掉了
        overlays.append({"index": i, "type": ov["type"], "png": cards.get(("overlay", i)),
                         "start": round(t, 3), "end": round(end, 3), "fields": ov.get("fields", {}),
                         "hide_captions": bool(ov.get("hide_captions"))})

    timeline = {"output": {**out, "fps": str(fps)}, "duration": float(t_out),
                "pieces": pieces, "overlays": overlays, "bgm": edl.get("bgm", [])}
    write_json(job / "timeline.json", timeline)
    return timeline


def overlay_windows(edl: dict, units: dict, sid: str, item: dict, cards: dict) -> list:
    """這個片段裡的疊加圖區間（素材時間）。疊加圖必須在片段階段就合成進畫面：
    留到最後一次編碼才疊，AMD 硬體編碼器會產生整段凍結畫格。"""
    out = []
    for i, ov in enumerate(edl.get("overlays", [])):
        png = cards.get(("overlay", i))
        if ov["type"] == "broll" or not png or ov["at"] not in units:
            continue
        u = units[ov["at"]]
        if u["source"] != sid or not (units[item["from"]]["start"] <= u["start"] <= units[item["to"]]["end"]):
            continue
        start = u["start"] + float(ov.get("offset", 0))
        out.append({"start": start, "end": start + float(ov.get("duration", 5)), "png": png})
    return sorted(out, key=lambda w: w["start"])


def broll_windows(edl: dict, units: dict, sid: str, item: dict) -> list:
    """這個片段裡的空鏡區間（素材時間）。at 指定從哪一句開始，in 是空鏡素材的起點秒數。"""
    out = []
    for ov in edl.get("overlays", []):
        if ov["type"] != "broll" or ov["at"] not in units:
            continue
        u = units[ov["at"]]
        if u["source"] != sid or not (units[item["from"]]["start"] <= u["start"] <= units[item["to"]]["end"]):
            continue
        start = u["start"] + float(ov.get("offset", 0))
        out.append({"start": start, "end": start + float(ov.get("duration", 5)),
                    "source": ov["source"], "in": float(ov.get("in", 0))})
    return sorted(out, key=lambda w: w["start"])


def snap_windows(windows: list, spans: list, a: float, b: float) -> None:
    """把空鏡區間的邊界對齊到既有的段落邊界，並吃掉太短的訪談空隙。

    不這樣做的話，空鏡前後會留下零點幾秒的訪談碎片，連續幾個看起來就像畫面在亂閃。
    """
    bounds = sorted({x for span in spans for x in span})
    for w in windows:
        for key in ("start", "end"):
            near = min(bounds, key=lambda x: abs(x - w[key]))
            if abs(near - w[key]) < MIN_SHOT:
                w[key] = near
        if w["start"] - a < MIN_SHOT:
            w["start"] = a
        if b - w["end"] < MIN_SHOT:
            w["end"] = b
    for w, nxt in zip(windows, windows[1:]):
        if nxt["start"] - w["end"] < MIN_SHOT:   # 兩段空鏡之間的訪談太短，直接接上
            w["end"] = nxt["start"]


def protect_words(cut0: float, cut1: float, words: list) -> tuple:
    """刪停頓不能切進任何一個字。

    靜音偵測會把很輕的字尾（遠距收音、講者收尾變小聲）當成靜音；只看音量刪，
    就會把整個字刪掉。跟辨識的字級時間比對，碰到字就把刪除區間縮回字的外面。
    """
    mid = (cut0 + cut1) / 2
    for ws, we in words:
        if we > cut0 and ws < cut1:
            if ws < mid:
                cut0 = max(cut0, we + 0.05)
            else:
                cut1 = min(cut1, ws - 0.05)
    return cut0, cut1


def split_span(span: tuple, x0: float, x1: float) -> list:
    """把一段 (s0, s1) 在 x0、x1 兩處切開，回傳仍有長度的小段。"""
    s0, s1 = span
    cuts = sorted({s0, s1} | {x for x in (x0, x1) if s0 < x < s1})
    return [(p, q) for p, q in zip(cuts, cuts[1:]) if q - p > 0.04]


def map_time(pieces: list, source: str, t: float):
    """素材時間 → 成片時間；落在被刪掉的停頓裡就對到下一段開頭。"""
    best = None
    for p in pieces:
        if p["type"] != "clip" or p["source"] != source:
            continue
        if p["src_start"] <= t <= p["src_end"]:
            return p["out_start"] + (t - p["src_start"])
        if p["src_start"] > t and (best is None or p["src_start"] < best["src_start"]):
            best = p
    if best is not None and best["src_start"] - t < 3:
        return best["out_start"]
    return None


def gpu_encoder(codec: str = "h264", qp: int = 18) -> list:
    """顯卡編碼器（AMD AMF / NVIDIA NVENC / Intel QSV）；沒有就回傳空的。qp 越小畫質越好。"""
    if not hasattr(gpu_encoder, "avail"):
        gpu_encoder.avail = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                           capture_output=True, text=True).stdout
    avail = gpu_encoder.avail
    tag = ["-tag:v", "hvc1"] if codec == "hevc" else []   # hvc1：YouTube／QuickTime 相容
    if f"{codec}_videotoolbox" in avail:                  # Apple 晶片（未在 AMD 機器上驗證過）
        return ["-c:v", f"{codec}_videotoolbox", "-q:v", str(max(1, min(100, round(100 - qp * 1.5)))), *tag]
    if f"{codec}_amf" in avail:
        return ["-c:v", f"{codec}_amf", "-usage", "transcoding", "-quality", "quality",
                "-rc", "cqp", "-qp_i", str(qp), "-qp_p", str(qp), "-qp_b", str(qp), *tag]
    if f"{codec}_nvenc" in avail:
        return ["-c:v", f"{codec}_nvenc", "-preset", "p5", "-rc", "constqp", "-qp", str(qp), *tag]
    if f"{codec}_qsv" in avail:
        return ["-c:v", f"{codec}_qsv", "-global_quality", str(qp), *tag]
    return []


def piece_cmd(p: dict, src: dict, out: dict, path: Path, job: Path, gpu: bool = True) -> list:
    w, h, fps = out["width"], out["height"], out["fps"]
    dur = p["frames"] / Fraction(fps)
    nsamp = round(float(dur) * SR)
    # 中繼片段一律用 H.264（qp 18 幾乎無損，且解碼比 HEVC 快，最後合成才不會拖慢）
    venc = (gpu and gpu_encoder("h264", 18)) or ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14"]
    enc = [*venc, "-pix_fmt", "yuv420p",
           "-video_track_timescale", "30000", "-c:a", "pcm_s16le", "-ar", str(SR), "-ac", "2"]
    # 注意：不要用 -frames:v 截斷。它在影像寫滿時就結束整個輸出，尾端還沒寫出的音訊會被丟掉，
    # 每段少幾毫秒、串接後累積成明顯的音畫不同步。改在濾鏡裡分別用 trim / atrim 精確截斷。
    if p["type"] == "card":
        fade = p["fade"]
        vf = (f"scale={w}:{h}:flags=lanczos,format=yuv420p,fade=t=in:st=0:d={fade},"
              f"fade=t=out:st={float(dur) - fade:.3f}:d={fade},trim=end_frame={p['frames']}")
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-loop", "1", "-framerate", fps, "-i", str(job / p["png"]),
                "-f", "lavfi", "-i", f"anullsrc=r={SR}:cl=stereo",
                "-vf", vf, "-af", f"atrim=end_sample={nsamp}", *enc, str(path)]
    zoom_f = ""
    if p["zoom"]:
        z = out["punch_in"]["zoom"]
        cx, cy = out["punch_in"]["center"]
        cw, ch = int(w / z) // 2 * 2, int(h / z) // 2 * 2
        x = int(min(max(cx * w - cw / 2, 0), w - cw))
        y = int(min(max(cy * h - ch / 2, 0), h - ch))
        zoom_f = f"crop={cw}:{ch}:{x}:{y},scale={w}:{h}:flags=lanczos,"
    vf = (f"fps={fps},scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,{zoom_f}setsar=1,format=yuv420p,"
          f"tpad=stop_mode=clone:stop_duration=1,trim=end_frame={p['frames']}")
    af = (f"aresample={SR},aformat=channel_layouts=stereo,afade=t=in:d=0.012,"
          f"afade=t=out:st={float(dur) - 0.012:.4f}:d=0.012,apad,atrim=end_sample={nsamp}")
    overlay = p.get("png_overlay")
    if overlay and any("_amf" in x for x in venc):
        # AMD 的 AMF 編碼器遇到「循環的圖片輸入」會整段凍結畫格（同樣的濾鏡鏈用 libx264 就正常），
        # 所以這種片段改用 CPU 編。別的平台沒這個問題，維持用顯卡。
        enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p",
               "-video_track_timescale", "30000", "-c:a", "pcm_s16le", "-ar", str(SR), "-ac", "2"]
    if p.get("broll"):
        # 畫面取自空鏡素材（裁切填滿畫面），聲音仍取自訪談
        br = p["broll"]
        bvf = (f"fps={fps},scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},"
               f"setsar=1,tpad=stop_mode=clone:stop_duration=1,trim=end_frame={p['frames']}")
        inputs = ["-ss", f"{br['in']:.4f}", "-t", f"{float(dur) + 1:.3f}", "-i", br["path"],
                  "-ss", f"{p['src_start']:.4f}", "-t", f"{float(dur) + 1:.3f}", "-i", src["path"]]
        # 進出空鏡用溶接：硬切在訪談片裡太跳，也讓觀眾知道畫面換了（空鏡接空鏡則直接切）
        fades = []
        if br.get("played", 0) < 0.01 and not br.get("cut_in"):
            fades.append(f"fade=t=in:st=0:d={DISSOLVE}:alpha=1")
        out_at = br.get("window", 0) - br.get("played", 0) - DISSOLVE
        if br.get("window") and out_at < float(dur) + 0.01 and not br.get("cut_out"):
            fades.append(f"fade=t=out:st={max(out_at, 0):.2f}:d={DISSOLVE}:alpha=1")
        if fades:
            graph = (f"[0:v]{bvf},format=rgba,{','.join(fades)}[bov];[1:v]{vf}[bg];"
                     f"[bg][bov]overlay=format=auto,format=yuv420p[base];[1:a]{af}[a]")
        else:
            graph = f"[0:v]{bvf},format=yuv420p[base];[1:a]{af}[a]"
        png_idx = 2
    else:
        inputs = ["-ss", f"{p['src_start']:.4f}", "-t", f"{float(dur) + 1:.3f}", "-i", src["path"]]
        graph = f"[0:v]{vf}[base];[0:a]{af}[a]"
        png_idx = 1
    if p.get("audio"):
        # 聲音取自整理過的音軌（prep_audio.py：降噪、音量對齊），影像不變
        inputs += ["-ss", f"{p['src_start']:.4f}", "-t", f"{float(dur) + 1:.3f}",
                   "-i", str((job / p["audio"]).resolve())]
        graph = graph.replace(f"[{png_idx - 1}:a]", f"[{png_idx}:a]")
        png_idx += 1
    if overlay:
        # 淡入淡出依「這一段在疊加區間裡的位置」計算（長停頓被刪掉時會切成好幾段）
        played, window = overlay["played"], overlay["window"]
        fades = []
        if played < 0.4:
            fades.append(f"fade=t=in:st=0:d={max(0.4 - played, 0.05):.2f}:alpha=1")
        out_at = window - 0.4 - played
        if out_at < float(dur):
            fades.append(f"fade=t=out:st={max(out_at, 0.0):.2f}:d=0.4:alpha=1")
        chain = ",".join(["format=rgba", *fades])
        inputs += ["-loop", "1", "-framerate", fps, "-t", f"{float(dur) + 1:.3f}",
                   "-i", str((job / overlay["png"]).resolve())]
        graph += (f";[{png_idx}:v]scale={w}:{h}:flags=lanczos,{chain}[ov];"
                  f"[base][ov]overlay=0:0:eof_action=pass[v]")
    else:
        graph += ";[base]null[v]"
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
            "-filter_complex", graph, "-map", "[v]", "-map", "[a]", *enc, str(path)]


def fingerprint(path: Path) -> str:
    """檔案內容的指紋：小檔（字卡 PNG）直接雜湊內容，大檔（影片、音訊）用大小＋修改時間。"""
    st = path.stat()
    if st.st_size < 8 * 1024 * 1024:
        return hashlib.sha1(path.read_bytes()).hexdigest()[:12]
    return f"{st.st_size}-{int(st.st_mtime)}"


def render_pieces(job: Path, timeline: dict, workers: int, gpu: bool = True) -> list:
    sources = load_sources(job)
    out = timeline["output"]
    pdir = job / "pieces"
    pdir.mkdir(exist_ok=True)
    jobs = []
    for p in timeline["pieces"]:
        if "broll" in p:  # 空鏡素材的實際路徑，piece_cmd 要用
            p["broll"]["path"] = sources[p["broll"]["source"]]["path"]
        key = {k: p[k] for k in ("type", "frames", "src_start", "source", "zoom", "png", "fade",
                                 "broll", "png_overlay") if k in p}
        key["out"] = {k: out[k] for k in ("width", "height", "fps", "punch_in")}
        # 快取要看檔案「內容」而不只是路徑：重跑 cards.py 或重做空鏡素材時檔名不變、內容變了，
        # 只看路徑會沿用舊片段，把前一版的人名條燒在別人身上
        used = [job / p[k] for k in ("png",) if k in p]
        if "png_overlay" in p:
            used.append(job / p["png_overlay"]["png"])
        if "broll" in p:
            used.append(Path(p["broll"]["path"]))
        if p["type"] == "clip":
            used.append(Path(sources[p["source"]]["path"]))
        if p.get("audio"):
            used.append(job / p["audio"])
        key["files"] = [fingerprint(f) for f in used]
        key["v"] = 8  # 片段編碼方式變更時遞增，讓舊快取失效
        key["gpu"] = bool(gpu and gpu_encoder())
        digest = hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]
        path = pdir / f"{digest}.mov"
        jobs.append((p, path))

    todo = [(p, path) for p, path in jobs if not path.exists()]
    print(f"片段 {len(jobs)} 個，需重新編碼 {len(todo)} 個", flush=True)

    def run(arg):
        p, path = arg
        tmp = path.with_suffix(".tmp.mov")
        subprocess.run(piece_cmd(p, sources.get(p.get("source"), {}), out, tmp, job, gpu), check=True)
        tmp.replace(path)

    with ThreadPoolExecutor(workers) as pool:
        for n, _ in enumerate(pool.map(run, todo), 1):
            if n % 20 == 0 or n == len(todo):
                print(f"  {n}/{len(todo)}", flush=True)
    return [path for _, path in jobs]


def concat(job: Path, paths: list, timeline: dict = None) -> Path:
    """串接中繼片段。

    清單裡一定要寫出每段的 duration：concat demuxer 自己從容器長度推算時，
    每個交界都會多塞一兩幀的空檔。聲音是另外算的、不會跟著變長，
    finish 的 fps 濾鏡又會照時間戳補幀，成片就愈到後面畫面愈慢。
    """
    lst = job / "pieces" / "concat.txt"
    fps = Fraction(timeline["output"]["fps"]) if timeline else None
    lines = []
    for i, path in enumerate(paths):
        lines.append(f"file '{path.resolve().as_posix()}'\n")
        if fps is not None:
            lines.append("duration %.6f\n" % (timeline["pieces"][i]["frames"] / float(fps)))
    lst.write_text("".join(lines), encoding="utf-8")
    body = job / "body.mov"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", str(body)], check=True)
    return body


# 注意：不要用 dynaudnorm，它和影像一起處理時會把開頭幾秒的音訊丟掉（片頭配樂因此消失）


def measure_loudness(body: Path, chain: str, target: float) -> dict:
    res = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(body), "-vn", "-af",
                          f"{chain},loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json",
                          "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8")
    text = res.stderr
    return json.loads(text[text.rindex("{"): text.rindex("}") + 1])


def speech_chain(body: Path, target: float) -> tuple:
    """人聲處理，回傳 (混音前的處理, 混音後的響度增益 dB)。降噪與各素材音量對齊已在 prep_audio 做完。

    響度標準化一定要放在混音之後：loudnorm 有數秒的前瞻緩衝，而 amix 是依到達順序混音，
    放在混音前會把配樂整整往後推兩秒（字卡的音效因此對不上）。
    """
    raw = measure_loudness(body, "anull", target)
    gain = min(max(-20 - float(raw["input_i"]), -10), 30)
    comp = f"volume={gain:.2f}dB,acompressor=threshold=0.063:ratio=2.5:attack=15:release=250:knee=4"
    m = measure_loudness(body, comp, target)
    # 不要用 loudnorm 濾鏡做最後的標準化：它會把取樣率拉到 192 kHz，時間戳還會出現空洞，
    # 播放時聲音跳過去、畫面追趕，看起來像被快轉。linear 模式本來就只是固定增益，直接用 volume。
    return comp, target - float(m["input_i"]) + float(m["target_offset"])


AUDIO_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "audio"


def decode_audio(path: Path, af: str = "") -> np.ndarray:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path)]
    if af:
        cmd += ["-af", af]
    raw = subprocess.run(cmd + ["-ac", "2", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, 2).copy()


def speech_gate(body: Path, t0: float, t1: float) -> tuple:
    """回傳 (每 20 ms 一格的「有人在講話」布林陣列, 格長秒數)，範圍是成片 t0～t1。

    門檻依這段本身的音量分布自動決定：現場側錄的環境音很大，用固定門檻會一直判成有人講話。
    """
    hop, rate = 0.02, 8000
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t0:.3f}",
                          "-t", f"{t1 - t0:.3f}", "-i", str(body), "-vn", "-ac", "1", "-ar", str(rate),
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.float32)
    n = int(rate * hop)
    fr = x[: len(x) // n * n].reshape(-1, n)
    db = 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1)) + 1e-9)
    floor, loud = np.percentile(db, 10), np.percentile(db, 95)
    on = db > floor + 0.35 * (loud - floor)
    # 字與字之間的短空檔不算停頓，免得音樂在一句話裡忽大忽小（抽氣感）
    hold = int(0.45 / hop)
    last = -hold - 1
    out = np.zeros_like(on)
    for i, v in enumerate(on):
        if v:
            last = i
        out[i] = i - last <= hold
    return out, hop


def bgm_layer(job: Path, timeline: dict, spec: dict, body: Path, total: int) -> np.ndarray:
    """一段鋪底背景音樂：從 from 項目開頭鋪到 to 項目結尾，講話時自動壓低。

    spec 欄位：music（曲名）、from / to（timeline 項目序號，含頭含尾）、gain（dB，整體音量）、
    duck（dB，有人講話時再壓低多少）、fade_in / fade_out（秒）、start_at（從曲子第幾秒開始）、
    loop_from / loop_to（曲子不夠長時，重複播放的區間；避開曲尾的淡出）。
    """
    items = [p for p in timeline["pieces"] if spec["from"] <= p["item"] <= spec["to"]]
    t0, t1 = min(p["out_start"] for p in items), max(p["out_end"] for p in items)
    length = int((t1 - t0) * SR)
    # 在人聲頻段挖一點：1～3 kHz 是字詞清晰度的關鍵，音樂讓出來講話才聽得清楚
    track = decode_audio(find_audio(job, spec["music"]),
                         "equalizer=f=1800:t=q:w=1.3:g=-5,highpass=f=90")
    # loop_to：曲子本身結尾的淡出不要拿來接，接點要落在音量穩定的地方
    loop_to = int(float(spec.get("loop_to", len(track) / SR)) * SR)
    clip = track[int(float(spec.get("start_at", 0)) * SR):loop_to].copy()
    seg = track[int(float(spec.get("loop_from", 0)) * SR):loop_to]
    # 曲子不夠長就接回去（交叉淡化 3 秒，接點聽不出來）
    xf = 3 * SR
    while len(clip) < length:
        ramp = np.linspace(0, 1, xf)[:, None]
        clip[-xf:] = clip[-xf:] * (1 - ramp) + seg[:xf] * ramp
        clip = np.concatenate([clip, seg[xf:]])
    clip = clip[:length] * 10 ** (float(spec.get("gain", -20)) / 20)

    gain_db = np.zeros(length, dtype=np.float32)
    if body is not None and float(spec.get("duck", 0)):
        on, hop = speech_gate(body, t0, t1)
        target = np.where(on, float(spec["duck"]), 0.0)
        # 壓下去要快（0.15 秒）、放回來要慢（0.9 秒），聽起來才自然
        a_dn, a_up = np.exp(-hop / 0.15), np.exp(-hop / 0.9)
        cur, smooth = 0.0, np.zeros_like(target)
        for i, v in enumerate(target):
            a = a_dn if v < cur else a_up
            cur = a * cur + (1 - a) * v
            smooth[i] = cur
        grid = np.arange(len(smooth)) * hop
        gain_db = np.interp(np.arange(length) / SR, grid, smooth).astype(np.float32)
    clip *= (10 ** (gain_db / 20))[:, None]

    fi, fo = int(float(spec.get("fade_in", 2)) * SR), int(float(spec.get("fade_out", 3)) * SR)
    if fi:
        clip[:fi] *= np.linspace(0, 1, fi)[:, None]
    if fo:
        clip[-fo:] *= np.linspace(1, 0, fo)[:, None]
    layer = np.zeros((total, 2), dtype=np.float32)
    start = int(t0 * SR)
    end = min(total, start + length)
    layer[start:end] = clip[: end - start]
    return layer


def build_music_bed(job: Path, timeline: dict, body: Path = None) -> Path:
    """把字卡音效與鋪底背景音樂放進一條與成片等長的音軌（48 kHz 立體聲）。

    不要在 ffmpeg 裡用 adelay + amix 把配樂疊上去：配樂那軌「晚到」時，
    amix 會讓輸出出現整段數位靜音。先合成好一條等長音軌再混，行為才穩定。
    """
    cues = [p for p in timeline["pieces"] if p["type"] == "card" and p.get("music")]
    bgm = timeline.get("bgm", [])
    if not cues and not bgm:
        return None
    total = int(timeline["duration"] * SR) + SR
    bed = np.zeros((total, 2), dtype=np.float32)
    for spec in bgm:
        bed += bgm_layer(job, timeline, spec, body, total)
    for p in cues:
        clip = decode_audio(find_audio(job, p["music"]))
        clip *= 10 ** (float(p.get("music_gain", 0)) / 20)
        # 音效比字卡早 0.12 秒進場，重音落在畫面切換那一格；字卡結束前淡出
        start = max(0, int((p["out_start"] - 0.12) * SR))
        fade_at = int((p["out_end"] - p["out_start"] - 0.3) * SR)
        if 0 < fade_at < len(clip):
            n = min(len(clip) - fade_at, int(0.8 * SR))
            clip[fade_at:fade_at + n] *= np.linspace(1, 0, n)[:, None]
            clip = clip[:fade_at + n]
        end = min(total, start + len(clip))
        bed[start:end] += clip[:end - start]
    out = job / "music_bed.wav"
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(bed, -1, 1) * 32767).astype(np.int16).tobytes())
    return out


def find_audio(job: Path, name: str) -> Path:
    """配樂檔。依序找：<job>/audio/ → 專案的 music/ → skill 的 assets/audio/。

    music/ 裡的檔名是「曲名 - 演出者.mp3」，所以 EDL 只要寫曲名就找得到。
    """
    bases = [job / "audio", job.parent.parent / "music", Path("music"), AUDIO_ASSETS]
    for base in bases:
        for ext in (".wav", ".mp3", ".m4a", ".flac"):
            if (base / f"{name}{ext}").exists():
                return base / f"{name}{ext}"
    for base in bases:                       # 再用「曲名 - 演出者」比對一次
        hit = sorted(base.glob(f"{name} - *")) if base.is_dir() else []
        if hit:
            return hit[0]
    raise FileNotFoundError(f"找不到配樂 {name}")


FONT_DIRS = {
    "win32": [Path(r"C:\Windows\Fonts"), Path.home() / "AppData/Local/Microsoft/Windows/Fonts"],
    "darwin": [Path("/Library/Fonts"), Path("/System/Library/Fonts"), Path.home() / "Library/Fonts"],
}.get(sys.platform, [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
                     Path.home() / ".local/share/fonts", Path.home() / ".fonts"])
FONT_PATTERNS = ["NotoSansTC*", "NotoSerifTC*"]


def ensure_fonts(job: Path) -> None:
    """把字幕用的字型複製到 <job>/fonts，讓 libass 一定找得到。"""
    dest = job / "fonts"
    dest.mkdir(exist_ok=True)
    for d in FONT_DIRS:
        for pattern in FONT_PATTERNS:
            for f in (d.rglob(pattern) if d.is_dir() else []):   # Linux 的字型放在子目錄裡
                if not (dest / f.name).exists():
                    shutil.copy(f, dest / f.name)


def finish(job: Path, timeline: dict, body: Path, preview: bool, gpu_final: bool = False) -> Path:
    """疊字幕、處理音訊、輸出成片。

    音訊一定要單獨先算成檔案，不要和影像放在同一個 filter_complex：
    兩邊同時跑時 ffmpeg 會隨機掉音訊畫格，播放時聲音跳過去、畫面追趕，看起來像突然快轉。
    """
    ensure_fonts(job)
    out = timeline["output"]
    fps = out["fps"]
    speech, gain = speech_chain(body, out["loudness_lufs"])

    # 第一步：只做音訊
    inputs, graph = ["-i", body.name], [f"[0:a]{speech},aresample={SR}[speech]"]
    bed = build_music_bed(job, timeline, body)
    music_eq = "highpass=f=120,lowpass=f=7000"
    if bed:
        # 配樂：一條與成片等長的音軌，不經過人聲的壓縮
        inputs += ["-i", bed.name]
        graph.append(f"[1:a]aresample={SR},aformat=channel_layouts=stereo,{music_eq}[music]")
        graph.append("[speech][music]amix=inputs=2:normalize=0:duration=first[mixed]")
    else:
        graph.append("[speech]anull[mixed]")
    # 響度標準化放在混音之後（放在前面會讓配樂慢兩秒）
    graph.append(f"[mixed]volume={gain:.2f}dB,alimiter=limit=0.89:level=false,aresample={SR}[aout]")
    # 記下各段增益，qc.py 用來算配樂在成片裡比人聲低多少
    write_json(job / "audio_levels.json", {"speech_chain": speech, "final_gain_db": round(gain, 2),
                                          "music_eq": music_eq if bed else None})
    wav = "audio_final.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
                    "-filter_complex", ";".join(graph), "-map", "[aout]",
                    "-c:a", "pcm_s16le", "-ar", str(SR), "-ac", "2", wav], check=True, cwd=job)

    # 第二步：影像。音訊直接帶過去，不再經過濾鏡
    #
    # fps 濾鏡會「照時間戳」補幀：body.mov 若有串接缺口，它會把缺口補滿，
    # 音訊卻不會跟著補，畫面就愈到後面愈慢。缺口已在 concat() 寫明每段長度時消除；
    # 這裡再確認幀數等於時間軸長度，確定 fps 濾鏡只是原樣通過、不會增減任何一幀。
    want = sum(p["frames"] for p in timeline["pieces"])
    got = int(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-count_packets",
                              "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0",
                              str(body)], capture_output=True, text=True).stdout.strip() or 0)
    if got != want:
        sys.exit(f"body.mov 有 {got} 幀，時間軸卻是 {want} 幀：串接有缺口，成片會影音不同步")
    vf = [f"fps={fps}"]
    if (job / "captions.ass").exists():
        vf.append("subtitles=filename=captions.ass:fontsdir=fonts")
    if preview:
        vf.append("scale=960:540:flags=lanczos")
    vf.append("format=yuv420p")
    name = "preview.mp4" if preview else "final.mp4"
    if preview:
        venc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "24"]
    elif gpu_final and gpu_encoder(out.get("codec", "h264")):
        # 顯卡編碼：速度快很多，同畫質下檔案略大
        venc = gpu_encoder(out.get("codec", "h264"), 22 if out.get("codec") == "hevc" else 20)
    else:
        venc = ["-c:v", "libx264", "-preset", "medium", "-crf", "19", "-profile:v", "high"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-stats_period", "60", "-y",
           "-i", body.name, "-i", wav,
           "-filter_complex", f"[0:v]{','.join(vf)}[vout]", "-map", "[vout]", "-map", "1:a",
           "-shortest", "-fps_mode", "cfr", "-r", fps, *venc, "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-ar", str(SR), "-movflags", "+faststart", name]
    (job / "finish_cmd.txt").write_text(subprocess.list2cmdline(cmd), encoding="utf-8")
    subprocess.run(cmd, check=True, cwd=job)
    return job / name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--no-gpu", action="store_true", help="中繼片段改用 CPU 編碼（預設有顯卡編碼器就用顯卡）")
    parser.add_argument("--cpu-final", action="store_true",
                        help="成片改用 CPU 編碼：慢很多，但同畫質下位元率少約四成，適合存檔母帶")
    args = parser.parse_args()

    edl = read_json(args.job / "edl.json")
    cards = {}
    manifest = args.job / "cards" / "cards.json"
    if manifest.exists():
        for c in read_json(manifest):
            cards[c["index"] if c["kind"] == "card" else ("overlay", c["index"])] = c["png"]
    missing = [i for i, it in enumerate(edl["timeline"]) if it["type"] == "card" and i not in cards]
    if missing:
        sys.exit(f"缺少字卡圖片（timeline 第 {missing} 項），請先執行 cards.py")
    # 疊加字卡是用「在 overlays 裡的序號」對應 PNG 的：EDL 中間插入或刪掉一個疊加（包括空鏡）
    # 之後序號就全部位移，若沒重跑 cards.py，人名條會套到別人身上。用內容指紋確認是同一版。
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from cards import card_sig, collect
    sigs = {(c["kind"], c["index"]): c.get("sig") for c in read_json(manifest)} if manifest.exists() else {}
    stale = [f"{kind} {idx}（{name}）" for kind, idx, name, fields in collect(edl)
             if sigs.get((kind, idx)) != card_sig(name, fields)]
    if stale:
        sys.exit("字卡圖片和 edl.json 對不上，請先重跑 cards.py：" + "、".join(stale))
    if not args.plan_only:
        # 各素材的降噪與音量對齊；已經是最新的會直接略過
        from prep_audio import prepare
        prepare(args.job, sorted({it["source"] for it in edl["timeline"] if it["type"] == "clip"}))

    timeline = plan(args.job, edl, cards)
    clips = [p for p in timeline["pieces"] if p["type"] == "clip"]
    print(f"成片長度 {fmt_ts(timeline['duration'])}，{len(timeline['pieces'])} 段"
          f"（影片 {len(clips)}、字卡 {len(timeline['pieces']) - len(clips)}），"
          f"疊加 {len(timeline['overlays'])} 個", flush=True)
    if args.plan_only:
        return
    if not args.no_gpu and gpu_encoder():
        print(f"中繼片段使用顯卡編碼：{gpu_encoder()[1]}", flush=True)
    paths = render_pieces(args.job, timeline, args.workers, not args.no_gpu)
    body = concat(args.job, paths, timeline)
    result = finish(args.job, timeline, body, args.preview, not args.cpu_final)
    print(f"完成 → {result}")


if __name__ == "__main__":
    sys.exit(main())
