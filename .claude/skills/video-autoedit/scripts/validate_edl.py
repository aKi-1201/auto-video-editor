"""檢查 edl.json，並產生給人看的「剪輯腳本.md」。

用法（在專案根目錄執行）:
    python validate_edl.py --job jobs/<job>

錯誤（ID 不存在、順序顛倒、找不到模板或配樂）會讓程式以非零結束；
覆蓋率不足（有句子既沒保留也沒列在 dropped）只提出警告。
"""
import argparse
import sys
from pathlib import Path

from common import fmt_ts, load_corrections, load_units, read_json

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "cards"


def has_audio(job: Path, name: str) -> bool:
    from render import find_audio
    try:
        find_audio(job, name)
        return True
    except FileNotFoundError:
        return False


def text_of(u: dict, fixes: dict) -> str:
    fix = fixes.get(u["id"], u["raw"])
    return fix.get("text", u["raw"]) if isinstance(fix, dict) else fix


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()

    edl = read_json(args.job / "edl.json")
    units = load_units(args.job)
    fixes = load_corrections(args.job)
    order = {}
    for u in sorted(units.values(), key=lambda u: (u["source"], u["start"])):
        order.setdefault(u["source"], []).append(u["id"])

    errors, warnings, used, rows, total = [], [], {}, [], 0.0
    starts = []  # 每個 timeline 項目在成片中的預估起點

    def span(a, b, where):
        if a not in units or b not in units:
            errors.append(f"{where}：找不到句子 {a if a not in units else b}")
            return []
        if units[a]["source"] != units[b]["source"]:
            errors.append(f"{where}：{a} 與 {b} 不是同一個素材")
            return []
        ids = order[units[a]["source"]]
        i, j = ids.index(a), ids.index(b)
        if i > j:
            errors.append(f"{where}：{a} 在 {b} 之後")
            return []
        return ids[i: j + 1]

    for n, it in enumerate(edl["timeline"]):
        where = f"timeline[{n}]"
        starts.append(total)
        if it["type"] == "clip":
            ids = span(it["from"], it["to"], where)
            if not ids:
                continue
            if not it.get("reason"):
                warnings.append(f"{where}：沒有 reason")
            for uid in ids:
                if uid in used:
                    warnings.append(f"{uid} 在 {used[uid]} 與 {where} 重複使用")
                used[uid] = where
            dur = units[ids[-1]]["end"] - units[ids[0]]["start"]
            first, last = text_of(units[ids[0]], fixes), text_of(units[ids[-1]], fixes)
            preview = first if len(ids) == 1 else f"{first[:24]} …… {last[-16:]}"
            rows.append(f"| {fmt_ts(total)} | 片段 {ids[0]}～{ids[-1]}（{len(ids)} 句，約 {dur:.0f} 秒） | {preview} | {it.get('reason', '')} |")
            total += dur
        elif it["type"] == "card":
            name = it["template"]
            if not ((args.job / "templates" / f"{name}.html").exists() or (ASSETS / f"{name}.html").exists()):
                errors.append(f"{where}：找不到字卡模板 {name}")
            if it.get("music") and not has_audio(args.job, it["music"]):
                errors.append(f"{where}：找不到配樂 {it['music']}")
            fields = "、".join(str(v).replace("\n", " ") for v in it.get("fields", {}).values())
            rows.append(f"| {fmt_ts(total)} | 字卡 {name}（{it['duration']} 秒） | {fields} | {it.get('reason', '')} |")
            total += float(it["duration"])
        else:
            errors.append(f"{where}：未知類型 {it['type']}")

    dropped_rows = []
    for n, d in enumerate(edl.get("dropped", [])):
        ids = span(d["from"], d["to"], f"dropped[{n}]")
        for uid in ids:
            used.setdefault(uid, f"dropped[{n}]")
        if ids:
            dropped_rows.append(f"| {ids[0]}～{ids[-1]}（{len(ids)} 句） | {text_of(units[ids[0]], fixes)[:30]} | {d.get('reason', '')} |")

    for n, ov in enumerate(edl.get("overlays", [])):
        if ov["at"] not in units:
            errors.append(f"overlays[{n}]：找不到句子 {ov['at']}")
        elif not str(used.get(ov["at"], "")).startswith("timeline"):
            errors.append(f"overlays[{n}]：{ov['at']} 沒有被保留在成片中")

    starts.append(total)
    bgm_rows = []
    for n, b in enumerate(edl.get("bgm", [])):
        if not 0 <= b["from"] <= b["to"] < len(edl["timeline"]):
            errors.append(f"bgm[{n}]：from／to 是 timeline 的項目序號（0～{len(edl['timeline']) - 1}），"
                          f"且 from 不能大於 to")
            continue
        if not has_audio(args.job, b["music"]):
            errors.append(f"bgm[{n}]：找不到配樂 {b['music']}")
        bgm_rows.append(f"- {b['music']}：{fmt_ts(starts[b['from']])}～{fmt_ts(starts[b['to'] + 1])}"
                        f"（timeline[{b['from']}]～[{b['to']}]）{b.get('reason', '')}")

    missing = [uid for uid in units if uid not in used]
    if missing:
        warnings.append(f"{len(missing)} 句既沒保留也沒列在 dropped：{', '.join(missing[:12])}{' …' if len(missing) > 12 else ''}")

    md = ["# 剪輯腳本", "", f"預估長度 {fmt_ts(total)}（刪除長停頓前）", "",
          "| 時間 | 內容 | 開頭／結尾 | 理由 |", "|---|---|---|---|", *rows, "",
          "## 刪除的段落", "", "| 範圍 | 開頭 | 理由 |", "|---|---|---|", *dropped_rows, ""]
    if bgm_rows:
        md += ["## 鋪底配樂", "", *bgm_rows, ""]
    if edl.get("overlays"):
        md += ["## 疊加字卡", ""]
        md += [f"- {ov['type']} @ {ov['at']}：{'、'.join(str(v) for v in ov.get('fields', {}).values())}"
               for ov in edl["overlays"]]
    (args.job / "剪輯腳本.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    for w in warnings:
        print(f"警告：{w}")
    for e in errors:
        print(f"錯誤：{e}")
    print(f"預估長度 {fmt_ts(total)}，保留 {sum(1 for v in used.values() if v.startswith('timeline'))} 句 → 剪輯腳本.md")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
