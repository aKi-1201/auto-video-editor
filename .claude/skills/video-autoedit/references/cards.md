# 字卡模板

字卡是 1920×1080 的 HTML 頁面，`cards.py` 用 Edge／Chrome 的無頭模式截成 PNG。
全畫面字卡有自己的底色；疊加字卡背景透明，只畫出面板。

## 共用模板（assets/cards/）

| 模板 | 類型 | 欄位 |
|---|---|---|
| `title` | 全畫面 | series、episode、title、kicker、byline、date、place |
| `chapter` | 全畫面 | number、label、title、note |
| `question` | 全畫面 | mark（預設「問」）、number、text |
| `end` | 全畫面 | series、thanks（預設「感謝收看」）、next、credits（「職稱：姓名」一行一筆）、footer |
| `quote` | 疊加 | text、speaker、plate_width（預設 860） |
| `lower_third` | 疊加 | name、title |

沒填的欄位會自動隱藏（CSS `.opt:empty`）。文字中的 `\n` 會變成換行。

## 風格「紙本印記」

- 暖白紙色底、思源宋體標題、思源黑體內文、朱紅方印為識別元素。
- `edl.json` 的 `style.theme`：`paper`（紙本，預設）或 `night`（夜談，深色）。
- `style.accent`：主色，建議 `#C23B22` 朱紅、`#1F5A4C` 墨綠、`#2B4A86` 藏青、`#8A5A14` 赭黃。深色主題會自動換成提亮版本。
- 單一字卡可以在 `fields` 裡覆寫 `theme`、`accent`。

## 為單支影片設計專用模板

放在 `jobs/<job>/templates/<name>.html`，EDL 的 `template`（全畫面）或 overlay 的 `type` 用同一個名字。

寫法：

```html
<!doctype html>
<html lang="zh-Hant-TW">
<head>
<meta charset="utf-8">
<link rel="stylesheet" href="base.css">   <!-- 共用字型與顏色變數 -->
<style>
.card { background: transparent; display: flex; justify-content: flex-end; }  /* 疊加類：透明底 */
.plate { height: 1080px; background: var(--bg); color: var(--ink); }
</style>
</head>
<body>
<div class="card" style="{{theme_vars}}">          <!-- 注入 --bg --ink --muted --rule --accent -->
  <div class="plate" style="width: {{plate_width}}px;">{{text}}</div>
</div>
</body>
</html>
```

- `{{欄位}}` 會被 HTML 跳脫後填入；`items` 欄位（一行一項）會自動產生 `{{items_html}}`（`<li>` 清單）。
- 可用的 class：`.serif`（宋體）、`.muted`（次要文字色）、`.seal`（主色方塊）、`.opt`（空的時候隱藏）。
- 疊加面板的欄位裡有 `plate_width` 時，字幕會自動移到面板左側。

設計原則：

- 先看畫面再決定面板位置與寬度，**不要擋到人臉**。
- 中文直排用 `writing-mode: vertical-rl`，很適合古文、詩詞。
- 標點處換行用 `word-break: keep-all`，避免「大／家」這種拆字。
- 字級：全畫面標題 100～130px、疊加面板主文 60～80px、說明 36～48px。
- 做完一定要拼成總覽圖看過（見 SKILL.md 品質檢查）。

## 專用模板的點子

依內容設計，放在 `jobs/<job>/templates/`：

- 直排金句：引文直排，旁邊加印章或出處。
- 生字／名詞卡：大字＋注音或原文＋解釋。
- 清單卡：步驟、重點，欄位 `items` 一行一項（`cards.py` 會自動編號）。
- 帶照片的片頭：欄位名稱以 `_img` 結尾就會被當成圖片路徑。
