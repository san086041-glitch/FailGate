"""README 素材用：截 failgate-demo 的真实 GitHub 页面，拼成两段动图。

- github-flow：出题 → 作弊 PR 被驳回 → 真修复通过（issue #2、PR #5、PR #16）；
- full-loop：issue 进来 → 封存考卷 → /failgate fix → 修复 Agent 开 PR → 通过核验 → 合并
  （#21、#22）。

只截公开页面上已有的内容（未登录视角），每一帧都是真实元素截图；
唯一加上去的是顶部的字幕条和编号。GitHub Actions 里跑（.github/workflows/readme-media.yml）。

    python scripts/media/web_shots.py --out docs/media/out
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from playwright.sync_api import Page

DEMO = "https://github.com/san086041-glitch/failgate-demo"
BOT = "failgate-dev-jian"


@dataclass(frozen=True)
class Shot:
    key: str
    url: str
    selectors: tuple[str, ...]  # 依次尝试，GitHub 页面改版时有退路
    has_text: str | None
    caption_en: str
    caption_zh: str
    open_details: tuple[str, ...] = ()  # 截图前展开这些 <summary> 文字开头的折叠块
    also: tuple[tuple[str, str], ...] = ()  # 和主元素合起来截（选择器, 包含的文字）


SHOTS = (
    Shot("issue", f"{DEMO}/issues/2",
         ("[data-testid=issue-body]", ".js-comment-container"), None,
         "① A user reports a bug: slugify() returns 'hello---world'",
         "① 用户报 bug：slugify() 返回了 'hello---world'"),
    Shot("sealed", f"{DEMO}/issues/2",
         ("[data-testid^=comment-viewer-outer-box]", ".js-comment-container"), f"{BOT} commented",
         "② FailGate reproduces it and seals a failing test (L2 + sha256 receipt)",
         "② FailGate 复现它，并封存一个会失败的测试（L2 + sha256 收据）",
         ("Failing test for the repository",)),
    Shot("cheat", f"{DEMO}/pull/5/files",
         (".file.js-file", "[data-testid=diff-file]", "#files"), None,
         "③ A PR says “Fixes #2” — but only edits the test",
         "③ 一个 PR 声称 “Fixes #2”——其实只改了测试"),
    Shot("refuted", f"{DEMO}/pull/5",
         (".js-comment-container",), BOT,
         "④ REFUTED: the exam was tampered with and still fails on the sealed version",
         "④ 驳回：考卷被改过，封存版本上仍然失败"),
    Shot("verified", f"{DEMO}/pull/16",
         (".js-comment-container",), "FailGate verification",
         "⑤ A real fix → VERIFIED, plus how strong the exam is",
         "⑤ 真正的修复 → 通过验收，并给出考卷强度"),
)

COMMENT = "[data-testid^=comment-viewer-outer-box]"
LOOP = (
    Shot("loop-issue", f"{DEMO}/issues/21", ("[data-testid=issue-body]",), None,
         "① An issue comes in: a crash in config.parse()",
         "① issue 进来：config.parse() 崩溃"),
    Shot("loop-sealed", f"{DEMO}/issues/21", (COMMENT,), f"{BOT} commented",
         "② FailGate triages, reproduces and seals a failing test",
         "② FailGate 分诊、复现，并封存一个会失败的测试",
         ("Failing test for the repository",)),
    Shot("loop-fix", f"{DEMO}/issues/21", (COMMENT,), "san086041-glitch commented",
         "③ The maintainer comments /failgate fix",
         "③ 维护者评论 /failgate fix",
         also=((COMMENT, "FailGate fix agent"),)),
    Shot("loop-patch", f"{DEMO}/pull/22/files", (".file.js-file", "[data-testid=diff-file]"),
         "config.py",
         "④ The fixer agent opens PR #22: a source fix + the sealed test",
         "④ 修复 Agent 开出 PR #22：改源码，并附上封存的考卷"),
    Shot("loop-verified", f"{DEMO}/pull/22", (".js-comment-container",), "FailGate verification",
         "⑤ Graded against the sealed test → Accepted (exam strength: medium)",
         "⑤ 用封存的考卷阅卷 → 通过验收（考卷强度：中）"),
    Shot("loop-merged", f"{DEMO}/pull/22",
         ("[class*='PullRequestHeader']", "#partial-discussion-header", ".gh-header-show"), None,
         "⑥ The maintainer merges — issue #21 closes itself",
         "⑥ 维护者合并，issue #21 自动关闭"),
)
PAGE_RECT = """e => { const r = e.getBoundingClientRect();
    return [r.left + scrollX, r.top + scrollY, r.right + scrollX, r.bottom + scrollY]; }"""
STORIES = {"github-flow": SHOTS, "full-loop": LOOP}

W, H, BAR = 1000, 640, 64
FPS_MS = 80


def capture(page: Page, shot: Shot, path: Path) -> None:
    page.goto(shot.url, wait_until="load", timeout=60000)
    page.wait_for_timeout(1500)
    for summary in shot.open_details:
        page.evaluate(
            """(t) => { for (const s of document.querySelectorAll('summary'))
                 if (s.innerText.trim().startsWith(t)) s.parentElement.open = true; }""",
            summary)
    for sel in shot.selectors:
        loc = page.locator(sel)
        if shot.has_text:
            loc = loc.filter(has_text=shot.has_text)
        if loc.count():
            loc.first.scroll_into_view_if_needed()
            if not shot.also:
                loc.first.screenshot(path=str(path))
                return
            boxes = [loc.first] + [page.locator(sel).filter(has_text=txt).first
                                   for sel, txt in shot.also]
            rects = [b.evaluate(PAGE_RECT) for b in boxes]
            x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
            x1, y1 = max(r[2] for r in rects), max(r[3] for r in rects)
            page.screenshot(path=str(path), full_page=True,
                            clip={"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0})
            return
    raise SystemExit(f"{shot.key}: no selector matched on {shot.url}")


def _font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("NotoSansCJK-Bold.ttc", "NotoSansCJKsc-Bold.otf", "msyhbd.ttc",
                 "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    for p in Path("/usr/share/fonts").rglob("NotoSansCJK-Bold.ttc"):
        return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


def frames_for(img: Image.Image, caption: str, dark: bool) -> list[Image.Image]:
    """一个镜头：字幕条 + 截图；截图比画面高就慢慢往下平移，停在开头和结尾。"""
    bg = (13, 17, 23) if dark else (246, 248, 250)
    bar = (31, 111, 235)
    scale = (W - 48) / img.width
    shot = img.resize((W - 48, max(1, round(img.height * scale))), Image.LANCZOS)
    view = H - BAR - 24
    travel = max(0, shot.height - view)
    # 开头停 1.6 秒、平移 2.4 秒、结尾停 3 秒；不平移的镜头停 4 秒
    # （相同的帧 GIF 会合并，不占体积）
    pan = [round(travel * i / 30) for i in range(1, 31)] if travel else []
    steps = [0] * 20 + pan + [travel] * (38 if travel else 30)
    font = _font(24)
    out = []
    for y in steps:
        f = Image.new("RGB", (W, H), bg)
        d = ImageDraw.Draw(f)
        d.rectangle((0, 0, W, BAR), fill=bar)
        d.text((24, BAR // 2), caption, font=font, fill="white", anchor="lm")
        f.paste(shot.crop((0, y, shot.width, y + min(view, shot.height))), (24, BAR + 12))
        out.append(f)
    return out


def build_gif(story: tuple[Shot, ...], shots: dict[str, Path], lang: str, dark: bool,
              out: Path) -> None:
    frames: list[Image.Image] = []
    for s in story:
        img = Image.open(shots[s.key]).convert("RGB")
        frames += frames_for(img, s.caption_en if lang == "en" else s.caption_zh, dark)
    pal = [f.quantize(colors=128, method=Image.Quantize.MEDIANCUT) for f in frames]
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=FPS_MS, loop=0,
                optimize=True, disposal=1)
    print(f"{out}  {len(frames)} frames  {out.stat().st_size / 1e6:.1f} MB")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=Path("docs/media/out"))
    p.add_argument("--from-raw", action="store_true", help="用 out/raw 里已有的截图重新拼 GIF")
    a = p.parse_args()
    raw = a.out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    if a.from_raw:
        for scheme in ("light",):  # 深色版只是为了深色模式，省掉一半体积
            shots = {s.key: raw / f"{s.key}.{scheme}.png" for s in (*SHOTS, *LOOP)}
            for name, story in STORIES.items():
                for lang in ("en", "zh"):
                    build_gif(story, shots, lang, scheme == "dark",
                              a.out / f"{name}.{lang}.{scheme}.gif")
        return
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for scheme in ("light",):  # 深色版只是为了深色模式，省掉一半体积
            ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                                      device_scale_factor=2, color_scheme=scheme, locale="en-US")
            page = ctx.new_page()
            shots = {}
            for s in (*SHOTS, *LOOP):
                shots[s.key] = raw / f"{s.key}.{scheme}.png"
                capture(page, s, shots[s.key])
            ctx.close()
            for name, story in STORIES.items():
                for lang in ("en", "zh"):
                    build_gif(story, shots, lang, scheme == "dark",
                              a.out / f"{name}.{lang}.{scheme}.gif")
        browser.close()


if __name__ == "__main__":
    main()
