"""各腳本共用的小工具：讀寫 job 檔案、時間格式、字級時間對齊。"""
import difflib
import json
from pathlib import Path

SENTENCE_END = set("。！？!?；;")
SOFT_BREAK = set("，,、：:")
PUNCT = SENTENCE_END | SOFT_BREAK | set("「」『』（）()《》〈〉…—．. 　\"'")


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_sources(job: Path) -> dict:
    return {s["id"]: s for s in read_json(job / "sources.json")["sources"]}


def load_units(job: Path) -> dict:
    return {u["id"]: u for u in read_json(job / "units.json")["units"]}


def load_corrections(job: Path) -> dict:
    """校稿結果：corrections.json 與 corrections-*.json（可分批撰寫）依檔名順序合併，後者覆蓋前者。"""
    merged = {}
    for path in sorted(job.glob("corrections*.json")):
        merged.update(read_json(path))
    return merged


def fmt_ts(sec: float) -> str:
    """給人看的時間：mm:ss.s（超過一小時加上小時）。"""
    sec = max(sec, 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:04.1f}" if h else f"{int(m):02d}:{s:04.1f}"


def char_times(unit: dict) -> list:
    """把 unit 的字級（token）時間攤成每個字元一組 [start, end]，對應 unit["raw"]。"""
    times = []
    for w in unit["words"]:
        text = w["word"].strip()
        if not text:
            continue
        span = (w["end"] - w["start"]) / len(text)
        times += [[w["start"] + i * span, w["start"] + (i + 1) * span] for i in range(len(text))]
    return times


def align_times(raw: str, raw_times: list, fixed: str, start: float, end: float) -> list:
    """校稿後的文字與辨識原文對齊，回傳 fixed 每個字元的 [start, end]。

    對得上的字沿用原文時間；對不上的（改字、補字）依前後已知時間線性內插。
    """
    out = [None] * len(fixed)
    sm = difflib.SequenceMatcher(a=raw, b=fixed, autojunk=False)
    if raw_times and sm.ratio() < 0.6:
        # 辨識錯得太多（例如古文朗讀）：零星對上的字常是位置錯誤的同音字，反而扭曲時間。
        # 改用「按字數比例」對應：辨識結果每個 token 大致對應一個音節，節奏仍然可信。
        content = [i for i, c in enumerate(fixed) if c not in PUNCT]
        n, m = len(content), len(raw_times)
        for k, i in enumerate(content):
            j = min(m - 1, int(k * m / max(n, 1)))
            out[i] = list(raw_times[j])
        last = [start, start]
        for i in range(len(fixed)):  # 標點沿用前一個字的結束時間
            if out[i] is None:
                out[i] = [last[1], last[1]]
            last = out[i]
        return out
    for a, b, n in sm.get_matching_blocks():
        for k in range(n):
            if a + k < len(raw_times):
                out[b + k] = list(raw_times[a + k])
    # 內插空缺
    known = [i for i, t in enumerate(out) if t]
    if not known:
        span = (end - start) / max(len(fixed), 1)
        return [[start + i * span, start + (i + 1) * span] for i in range(len(fixed))]
    for i in range(len(fixed)):
        if out[i]:
            continue
        prev = max((k for k in known if k < i), default=None)
        nxt = min((k for k in known if k > i), default=None)
        t0 = out[prev][1] if prev is not None else start
        t1 = out[nxt][0] if nxt is not None else end
        lo = prev if prev is not None else -1
        hi = nxt if nxt is not None else len(fixed)
        frac0 = (i - lo - 1) / (hi - lo - 1)
        frac1 = (i - lo) / (hi - lo - 1)
        out[i] = [t0 + (t1 - t0) * frac0, t0 + (t1 - t0) * frac1]
    return out
