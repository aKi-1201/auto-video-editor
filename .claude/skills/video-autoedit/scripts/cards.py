"""依 edl.json 把字卡模板轉成 1920x1080 PNG（疊加類字卡背景透明）。

用法（在專案根目錄執行）:
    python cards.py --job jobs/<job>

模板搜尋順序：<job>/templates/<name>.html（這支影片專用，Claude 可自行設計）
             → skill 的 assets/cards/<name>.html（共用模板）。
模板用 {{欄位}} 填字，{{theme_vars}} 注入主題色。轉圖使用本機 Edge／Chrome 的無頭模式。
產生 <job>/cards/<編號>_<模板>.png，並寫入 <job>/cards/cards.json 供 render.py 使用。
"""
import argparse
import html
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from common import read_json, write_json

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "cards"

THEMES = {
    "paper": {"bg": "#F3EEE4", "ink": "#1B1A17", "muted": "#5E5A52", "rule": "#CFC7B8"},
    "night": {"bg": "#141311", "ink": "#F1ECE2", "muted": "#A9A396", "rule": "#3A3731"},
}
THEME_ALIASES = {"紙本": "paper", "夜談": "night"}
# 深色主題下把主色提亮，維持對比
NIGHT_ACCENT = {"#C23B22": "#E0623F", "#1F5A4C": "#5FA88E", "#2B4A86": "#88A6E0", "#8A5A14": "#D9A441"}
DEFAULTS = {"mark": "問", "thanks": "感謝收看", "plate_width": "860"}

# 字卡轉圖用的無頭瀏覽器；依序尋找，找不到才報錯
BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
BROWSER_CMDS = ["google-chrome", "chromium", "chromium-browser", "microsoft-edge"]


def find_browser():
    return (next((b for b in BROWSERS if os.path.exists(b)), None)
            or next((p for c in BROWSER_CMDS if (p := shutil.which(c))), None))


def theme_vars(theme: str, accent: str) -> str:
    theme = THEME_ALIASES.get(theme, theme)
    colors = dict(THEMES[theme])
    accent = accent.upper()
    colors["accent"] = NIGHT_ACCENT.get(accent, accent) if theme == "night" else accent
    return ";".join(f"--{k}:{v}" for k, v in colors.items())


def find_template(job: Path, name: str) -> Path:
    for base in (job / "templates", ASSETS):
        if (base / f"{name}.html").exists():
            return base / f"{name}.html"
    raise FileNotFoundError(f"找不到字卡模板 {name}.html")


def fill(template: str, fields: dict, tvars: str, job: Path = Path(".")) -> str:
    values = {**DEFAULTS, **{k: str(v) for k, v in fields.items()}}
    # 以 _img 結尾的欄位是圖片路徑（相對於 job 目錄），轉成瀏覽器可讀的 file:// 網址
    for k in list(values):
        if k.endswith("_img") and values[k]:
            values[k + "_html"] = html.escape((job / values[k]).resolve().as_uri(), quote=True)
    # 片尾卡的組合欄位
    nxt = values.get("next", "")
    values["next_html"] = (f'<div class="rule"></div><div class="serif">{html.escape(nxt)}</div>' if nxt else "")
    credits = [c.split("：", 1) for c in values.get("credits", "").splitlines() if "：" in c]
    values["credits_html"] = "".join(
        f'<div class="credit"><div class="role muted">{html.escape(r)}</div>'
        f'<div class="name serif">{html.escape(n)}</div></div>' for r, n in credits)
    # 清單類模板：items 一行一項，編號自動產生
    values["items_html"] = "".join(
        f'<li><span class="n serif">{i}</span><span>{html.escape(t)}</span></li>'
        for i, t in enumerate((x for x in values.get("items", "").splitlines() if x.strip()), 1))
    out = template.replace("{{theme_vars}}", tvars)
    for key, val in values.items():
        raw = key.endswith("_html")
        out = out.replace("{{" + key + "}}", val if raw else html.escape(val).replace("\n", "<br>"))
    # 沒提供的欄位清空（配合 .opt:empty 隱藏）
    while "{{" in out:
        a = out.index("{{")
        out = out[:a] + out[out.index("}}", a) + 2:]
    return out


def screenshot(browser: str, html_path: Path, png: Path, profile: str, scale: float = 1.0) -> None:
    subprocess.run([
        browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
        f"--force-device-scale-factor={scale:g}", "--window-size=1920,1080",
        "--default-background-color=00000000", "--virtual-time-budget=3000",
        f"--user-data-dir={profile}", f"--screenshot={png.resolve()}", html_path.resolve().as_uri(),
    ], check=True, capture_output=True, timeout=120)
    if not png.exists():
        raise RuntimeError(f"瀏覽器沒有產生 {png}")


def card_sig(template: str, fields: dict) -> str:
    """字卡內容的指紋。render.py 用它確認 cards.json 和 edl.json 是同一版。"""
    import hashlib, json
    raw = json.dumps({"template": template, "fields": fields}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def collect(edl: dict) -> list:
    """列出 EDL 中所有需要轉圖的字卡：(類別, 索引, 模板, 欄位)。"""
    items = []
    for i, it in enumerate(edl["timeline"]):
        if it["type"] == "card":
            items.append(("card", i, it["template"], it.get("fields", {})))
    for i, ov in enumerate(edl.get("overlays", [])):
        if ov["type"] == "broll":  # 空鏡是影片，不需要轉圖
            continue
        items.append(("overlay", i, ov.get("template", ov["type"]), ov.get("fields", {})))
    return items


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()

    browser = find_browser()
    if not browser:
        sys.exit("找不到 Chrome／Edge／Chromium，無法轉出字卡")
    edl = read_json(args.job / "edl.json")
    style = edl.get("style", {})
    # 模板是 1920x1080 的版面；輸出更高解析度時用瀏覽器縮放，文字才不會糊
    scale = edl.get("output", {}).get("height", 1080) / 1080
    out_dir = args.job / "cards"
    html_dir = out_dir / "_html"
    html_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(ASSETS / "base.css", html_dir / "base.css")

    manifest = []
    with tempfile.TemporaryDirectory() as profile:
        for kind, idx, name, fields in collect(edl):
            tvars = theme_vars(fields.get("theme", style.get("theme", "paper")),
                               fields.get("accent", style.get("accent", "#C23B22")))
            stem = f"{kind}{idx:03d}_{name}"
            page = html_dir / f"{stem}.html"
            page.write_text(fill(find_template(args.job, name).read_text(encoding="utf-8"), fields, tvars, args.job),
                            encoding="utf-8")
            png = out_dir / f"{stem}.png"
            screenshot(browser, page, png, profile, scale)
            manifest.append({"kind": kind, "index": idx, "template": name, "sig": card_sig(name, fields),
                             "png": str(png.relative_to(args.job))})
            print(f"{stem}.png", flush=True)
    write_json(out_dir / "cards.json", manifest)


if __name__ == "__main__":
    sys.exit(main())
