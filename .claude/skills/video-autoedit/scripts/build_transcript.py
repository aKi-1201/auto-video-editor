"""把辨識結果整理成句子單位（unit），產生給 Claude 讀的 transcript.md。

用法（在專案根目錄執行）:
    python build_transcript.py --job jobs/<job> --model large-v3

產生:
    <job>/units.json       每句的 ID、時間、原文、字級時間（程式用）
    <job>/transcript.md    ID + 起始時間 + 文字 + 停頓標記（給 Claude 讀）

斷句規則：句末標點、超過 --gap 秒的停頓、或長度超過 --max-chars 時在逗號處切開。
文字會用 OpenCC 做簡轉繁（字元層級，不做詞彙轉換，以免改到古文）。
"""
import argparse
import sys
from pathlib import Path

from common import PUNCT, SENTENCE_END, SOFT_BREAK, fmt_ts, load_sources, read_json, write_json


def to_traditional():
    try:
        import opencc
        # s2tw：台灣字形（裡、為…），不做詞彙替換，以免改到古文
        return opencc.OpenCC("s2tw").convert
    except Exception:  # 沒裝 OpenCC 就原樣輸出
        return lambda s: s


def word_bounds(tokens: list) -> set:
    """回傳可以切開的 token 索引（該 token 的開頭剛好是一個詞的開頭）。用 jieba 斷詞。"""
    text = "".join(t["word"].strip() for t in tokens)
    try:
        import jieba
        import opencc
        simp = opencc.OpenCC("t2s").convert(text)
        seg = simp if len(simp) == len(text) else text
        starts, pos = set(), 0
        for w in jieba.cut(seg, HMM=False):
            starts.add(pos)
            pos += len(w)
    except ImportError:
        return set(range(1, len(tokens)))
    out, pos = set(), 0
    for i, t in enumerate(tokens):
        if i and pos in starts:
            out.add(i)
        pos += len(t["word"].strip())
    return out


def split_units(segments: list, gap: float, max_chars: int) -> list:
    """斷句：句末標點、長停頓；太長時在逗號後，或詞界中字間停頓最久處切開。"""
    words = [w for seg in segments for w in seg["words"] if w["word"].strip()]
    units, cur = [], []

    def length(ws):
        return sum(len(x["word"].strip()) for x in ws)

    for w in words:
        if cur and w["start"] - cur[-1]["end"] > gap:
            units.append(cur)
            cur = []
        cur.append(w)
        text = w["word"].strip()
        if text[-1] in SENTENCE_END:
            units.append(cur)
            cur = []
        elif length(cur) >= max_chars:
            lo = len(cur) // 3
            commas = [i for i in range(max(lo, 1), len(cur)) if cur[i - 1]["word"].strip()[-1] in SOFT_BREAK]
            bounds = [i for i in word_bounds(cur) if i >= max(lo, 1)]
            if commas:
                cut = commas[-1]
            elif bounds:
                cut = max(bounds, key=lambda i: cur[i]["start"] - cur[i - 1]["end"])
            else:
                continue  # 找不到詞界就先不切，等下一個停頓或標點
            units.append(cur[:cut])
            cur = cur[cut:]
    if cur:
        units.append(cur)
    return [u for u in units if u]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--gap", type=float, default=0.6, help="超過幾秒的停頓就斷句")
    parser.add_argument("--max-chars", type=int, default=28)
    parser.add_argument("--pause-mark", type=float, default=1.0, help="transcript.md 標出超過幾秒的停頓")
    parser.add_argument("--aux", action="store_true",
                        help="對照用：只輸出 transcript.<model>.md，不覆寫 units.json（難辨識的錄音可用第二個模型交叉比對）")
    args = parser.parse_args()

    convert = to_traditional()
    sources = load_sources(args.job)
    all_units, md = [], []
    for sid, src in sources.items():
        path = args.job / "words" / f"{sid}.{args.model}.json"
        if not path.exists():
            continue
        data = read_json(path)
        dur = src["duration"]
        md.append(f"## {sid} · {Path(src['path']).name} · {fmt_ts(dur)}")
        prev_end = 0.0
        for n, words in enumerate(split_units(data["segments"], args.gap, args.max_chars), 1):
            uid = f"{sid}-{n:04d}"
            raw = convert("".join(w["word"].strip() for w in words))
            unit = {"id": uid, "source": sid, "start": words[0]["start"], "end": words[-1]["end"],
                    "raw": raw, "words": words,
                    "low_conf": sum(w["prob"] < 0.5 for w in words) / len(words) > 0.3}
            all_units.append(unit)
            pause = unit["start"] - prev_end
            if pause >= args.pause_mark:
                md.append(f"          ⏸ {pause:.1f}s")
            flag = " ⚠" if unit["low_conf"] else ""
            md.append(f"{uid} {fmt_ts(unit['start'])} {raw}{flag}")
            prev_end = unit["end"]
        md.append("")

    header = [
        "# 逐字稿" + ("（對照用，ID 與主逐字稿不同，請用時間對照）" if args.aux else ""),
        "",
        f"模型：{args.model}。格式：句子 ID、起始時間、文字；⏸ 為停頓秒數；⚠ 表示辨識信心低。",
        "",
    ]
    if args.aux:
        (args.job / f"transcript.{args.model}.md").write_text("\n".join(header + md), encoding="utf-8")
        print(f"對照稿 → {args.job / f'transcript.{args.model}.md'}")
        return
    write_json(args.job / "units.json", {"model": args.model, "units": all_units})
    (args.job / "transcript.md").write_text("\n".join(header + md), encoding="utf-8")
    chars = sum(len(u["raw"]) for u in all_units)
    print(f"{len(all_units)} 句、{chars} 字 → {args.job / 'transcript.md'}")


if __name__ == "__main__":
    sys.exit(main())
