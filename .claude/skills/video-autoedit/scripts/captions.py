"""產生字幕：把校稿後的逐字稿對到成片時間軸。

用法（在專案根目錄執行，需先跑 render.py --plan-only 產生 timeline.json）:
    python captions.py --job jobs/<job>

輸入:
    units.json、timeline.json、edl.json
    corrections.json（選用）: {"A001-0012": "校正後文字"} 或
                              {"A001-0012": {"text": "…", "style": "classic"}}；text 為空字串代表這句不上字幕
輸出:
    captions.ass（燒入用）、final.srt（上傳 YouTube 字幕用）

台灣字幕慣例：單行、每行最多約 16 字、去掉句末標點、句中逗號改空白、保留問號與驚嘆號。
"""
import argparse
import sys
from pathlib import Path

from common import align_times, char_times, load_corrections, load_units, read_json

BREAK = set("，,、。．；;：:…") | {" ", "　"}
KEEP_END = set("？！?!")
DROP = set("\"“”")
SPACE = "　"

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{styles}
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
# 名稱: (字型, 字級, 主色 &HAABBGGRR, 字距)
# 注意：libass 會把名為小寫 default 的樣式當成它內建的小字預設樣式，所以樣式名稱首字大寫
STYLES = {
    "Default": ("Noto Sans TC", 58, "&H00FFFFFF", 1),
    "Classic": ("Noto Serif TC", 62, "&H00DCF0FF", 2),  # 古文原文：宋體、微暖白
}


def width(s: str) -> float:
    return sum(0.5 if ord(c) < 0x2E80 else 1 for c in s)


def phrases(text: str) -> list:
    """切成片語：[(起, 迄, 結尾要保留的標點)]，索引對應 text。"""
    out, start = [], None
    for i, c in enumerate(text):
        if c in BREAK or c in KEEP_END:
            if start is not None:
                out.append((start, i, c if c in KEEP_END else ""))
                start = None
            elif c in KEEP_END and out:
                out[-1] = (out[-1][0], out[-1][1], c)
        elif c in DROP:
            continue
        elif start is None:
            start = i
    if start is not None:
        out.append((start, len(text), ""))
    return out


def clean(s: str) -> str:
    return "".join(c for c in s if c not in DROP)


def split_long(text: str, a: int, b: int, p: str, max_w: float) -> list:
    """沒有標點又太長的片語：平均切段，切點挪到最近的詞界（jieba 斷詞）。"""
    n = int(-(-width(text[a:b]) // max_w))
    try:
        import jieba
        seg = text[a:b]
        try:  # jieba 詞典是簡體：先轉簡體斷詞（逐字轉換，長度不變），詞界再對回原文
            import opencc
            simp = opencc.OpenCC("t2s").convert(seg)
            seg = simp if len(simp) == len(seg) else seg
        except ImportError:
            pass
        bounds, pos = set(), a
        for w in jieba.cut(seg, HMM=False):
            pos += len(w)
            bounds.add(pos)
    except ImportError:
        bounds = set(range(a + 1, b))
    cuts, start = [], a
    for k in range(1, n):
        ideal = a + round((b - a) * k / n)
        cut = min((x for x in bounds if start < x < b), key=lambda x: abs(x - ideal), default=ideal)
        cuts.append(cut)
        start = cut
    edges = [a, *cuts, b]
    return [(x, y, p if y == b else "") for x, y in zip(edges, edges[1:]) if y > x]


def lines(text: str, max_w: float) -> list:
    """組成字幕行：[(顯示文字, 首字索引, 末字索引)]。

    先用最少的行數，再讓各行長度盡量平均（避免「長長長／短」的切法）。
    """
    parts = []
    for a, b, p in phrases(text):
        parts += split_long(text, a, b, p, max_w) if width(clean(text[a:b]) + p) > max_w else [(a, b, p)]
    if not parts:
        return []
    ws = [width(clean(text[a:b]) + p) for a, b, p in parts]
    n = len(parts)
    best = [(0, 0.0)] + [(10 ** 9, 0.0)] * n
    back = [0] * (n + 1)
    for j in range(1, n + 1):
        for i in range(j - 1, -1, -1):
            w = sum(ws[i:j]) + (j - i - 1)
            if w > max_w and j - i > 1:
                break
            cand = (best[i][0] + 1, max(best[i][1], w))
            if cand < best[j]:
                best[j], back[j] = cand, i
    groups, j = [], n
    while j > 0:
        groups.append(parts[back[j]:j])
        j = back[j]
    return [(SPACE.join(clean(text[a:b]) + p for a, b, p in g), g[0][0], g[-1][1] - 1) for g in reversed(groups)]


def map_time(pieces: list, source: str, t: float, item: int):
    """素材時間 → 成片時間（限同一個 timeline 片段）；落在刪掉的停頓裡就貼齊最近的保留段。"""
    after = before = None
    for p in pieces:
        if p["type"] != "clip" or p["source"] != source or p["item"] != item:
            continue
        if p["src_start"] <= t <= p["src_end"]:
            return p["out_start"] + (t - p["src_start"])
        if p["src_start"] > t and (after is None or p["src_start"] < after["src_start"]):
            after = p
        if p["src_end"] < t and (before is None or p["src_end"] > before["src_end"]):
            before = p
    if after is not None:
        return after["out_start"]
    return before["out_end"] if before is not None else None


def ts_ass(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ts_srt(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--max-chars", type=float, default=16)
    args = parser.parse_args()

    units = load_units(args.job)
    fixes = load_corrections(args.job)
    edl = read_json(args.job / "edl.json")
    tl = read_json(args.job / "timeline.json")
    pieces = tl["pieces"]
    order = {}
    for u in sorted(units.values(), key=lambda u: (u["source"], u["start"])):
        order.setdefault(u["source"], []).append(u["id"])

    cues = []
    for idx, item in enumerate(edl["timeline"]):
        if item["type"] != "clip":
            continue
        # 把這個片段內各句的文字串成連續字流（每字帶時間與樣式），
        # 再依句末標點、長停頓、樣式變化重新斷句——字幕不受逐字稿切句位置影響
        ids = order[item["source"]]
        stream = []
        for uid in ids[ids.index(item["from"]): ids.index(item["to"]) + 1]:
            u = units[uid]
            fix = fixes.get(uid, u["raw"])
            text, style = (fix.get("text", u["raw"]), fix.get("style", "default")) if isinstance(fix, dict) else (fix, "default")
            if not text:
                continue
            times = align_times(u["raw"], char_times(u), text, u["start"], u["end"])
            for c, (t0, t1) in zip(text, times):
                # 直接換算成成片時間：刪掉的停頓不再造成斷句
                o0 = map_time(pieces, item["source"], t0, idx)
                o1 = map_time(pieces, item["source"], t1, idx)
                if o0 is not None and o1 is not None:
                    stream.append((c, o0, max(o1, o0), style.capitalize()))
        sentences, cur = [], []
        for ch in stream:
            if cur and (ch[3] != cur[-1][3] or ch[1] - cur[-1][2] > 0.8):
                sentences.append(cur)
                cur = []
            cur.append(ch)
            if ch[0] in "。！？!?；;":
                sentences.append(cur)
                cur = []
        if cur:
            sentences.append(cur)
        for sent in sentences:
            text = "".join(c[0] for c in sent)
            for shown, i0, i1 in lines(text, args.max_chars):
                t0, t1 = sent[i0][1], sent[i1][2]
                if not shown.strip():
                    continue
                cues.append({"start": t0, "end": max(t1, t0 + 0.3), "text": shown, "style": sent[i0][3]})

    # 整理時間：最短 0.8 秒、句尾多留 0.4 秒，但不和下一句重疊
    cues.sort(key=lambda c: c["start"])
    for a, b in zip(cues, cues[1:] + [None]):
        limit = b["start"] - 0.04 if b else a["end"] + 0.4
        a["end"] = min(max(a["end"] + 0.4, a["start"] + 0.8), max(limit, a["start"] + 0.3))

    # 全螢幕字卡（例如金句卡）上不要再壓字幕
    hide = [o for o in tl["overlays"] if o.get("hide_captions")]
    cues = [c for c in cues if not any(c["start"] < o["end"] and c["end"] > o["start"] for o in hide)]

    # 右側面板類疊加（金句、生字卡、清單…）出現時，字幕移到面板左側置中
    panels = [o for o in tl["overlays"] if o["type"] == "quote" or "plate_width" in o["fields"]]
    for c in cues:
        widths = [int(o["fields"].get("plate_width", 860)) for o in panels
                  if c["start"] < o["end"] and c["end"] > o["start"]]
        if widths:
            c["margin_r"] = max(widths) + 40

    # edl.style.caption_scale：這支片的字幕放大倍率（預設 1.0＝58 px）。外框、陰影一起放大才不會顯得細
    scale = float(edl.get("style", {}).get("caption_scale", 1.0))
    styles = []
    for name, (font, size, color, spacing) in STYLES.items():
        styles.append(f"Style: {name},{font},{round(size * scale)},{color},&H000000FF,&H00101010,&H99000000,-1,0,0,0,"
                      f"100,100,{spacing},0,1,{3.4 * scale:.1f},{1.5 * scale:.1f},2,60,60,62,1")
    events = []
    srt = []
    for n, c in enumerate(cues, 1):
        mr = c.get("margin_r", 0)
        events.append(f"Dialogue: 0,{ts_ass(c['start'])},{ts_ass(c['end'])},{c['style']},,0,{mr},0,,{c['text']}")
        srt.append(f"{n}\n{ts_srt(c['start'])} --> {ts_srt(c['end'])}\n{c['text']}\n")
    # 不加 BOM、用 LF 換行，避免 ffmpeg 讀不到 [Script Info] 裡的 PlayRes
    with open(args.job / "captions.ass", "w", encoding="utf-8", newline="\n") as f:
        f.write(ASS_HEADER.format(styles="\n".join(styles)) + "\n".join(events) + "\n")
    with open(args.job / "final.srt", "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(srt))
    print(f"字幕 {len(cues)} 條 → captions.ass、final.srt")


if __name__ == "__main__":
    sys.exit(main())
