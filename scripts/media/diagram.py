"""README 的原理图：生成 docs/media/brand/how-it-works.{en,zh}.{light,dark}.svg。

    python scripts/media/diagram.py
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path("docs/media/brand")

THEMES = {
    "light": dict(bg="#ffffff", card="#f6f8fa", line="#d1d9e0", fg="#1f2328", sub="#59636e",
                  blue="#0969da", green="#1a7f37", red="#cf222e", amber="#9a6700",
                  purple="#8250df"),
    "dark": dict(bg="#0d1117", card="#151b23", line="#3d444d", fg="#f0f6fc", sub="#9198a1",
                 blue="#4493f8", green="#3fb950", red="#f85149", amber="#d29922",
                 purple="#ab7df8"),
}

TEXT = {
    "en": dict(
        s1="① Write the exam", s1a="Issue → reproduce in a", s1b="Docker sandbox",
        s1c="Agent writes a failing test", s1d="in the repo's own test dir",
        s1e="Judge: fails the way the", s1f="issue says (L2 evidence)",
        s2="② Seal it", s2a="sha256 + receipt (JSON)", s2b="append-only store",
        s2c="Verification always runs", s2d="the sealed version",
        s3="③ Grade any fix", s3a="PR says “Fixes #N” — from a human,",
        s3b="Claude Code, Codex, Copilot or FailGate’s own fixer",
        l1="Sealed test fails before, passes after", l2="No tampering: edits, skip/xfail, conftest",
        l3="No new failures in related tests", extra="+ exam strength (mutation) · hidden exam",
        v="Verdict", va="VERIFIED", vb="REFUTED", vc="INCONCLUSIVE", vd="posted on the PR",
        ve="+ hashed receipt",
        loop="refuted → reasons fed back to the fixer → retry (max 2 rounds)",
        foot1="Entry points", foot1a="GitHub App  ·  CLI  ·  MCP (Claude Code, Cursor)",
        foot2="Sandbox", foot2a="no network at run time · install via egress allowlist",
    ),
    "zh": dict(
        s1="① 出题", s1a="Issue → 在 Docker", s1b="沙箱里复现",
        s1c="Agent 在仓库自己的测试目录", s1d="写一个会失败的测试",
        s1e="判定器确认：失败方式就是", s1f="issue 说的问题（L2 证据）",
        s2="② 封存", s2a="sha256 + 证据收据", s2b="只加不改",
        s2c="核验永远跑", s2d="封存的版本",
        s3="③ 阅卷：核验任何修复", s3a="PR 声称 “Fixes #N”——人写的，",
        s3b="或 Claude Code、Codex、Copilot、自带的修复 Agent",
        l1="封存的测试：修复前失败、修复后通过", l2="无篡改：删改测试、skip/xfail、conftest",
        l3="相关的已有测试没有新增失败", extra="+ 考卷强度（变异测试）· 隐藏考卷",
        v="结论", va="通过验收", vb="驳回", vc="无法判定", vd="发到 PR 上",
        ve="+ 核验收据",
        loop="驳回 → 理由回传给修复 Agent → 重修（最多 2 轮）",
        foot1="入口", foot1a="GitHub App  ·  命令行  ·  MCP（Claude Code、Cursor）",
        foot2="沙箱", foot2a="运行时断网 · 安装阶段只能经白名单代理出网",
    ),
}

FONT = ("font-family=\"Segoe UI, -apple-system, BlinkMacSystemFont, 'PingFang SC', "
        "'Microsoft YaHei', Helvetica, Arial, sans-serif\"")


def svg(lang: str, theme: str) -> str:
    c, x = THEMES[theme], TEXT[lang]
    parts: list[str] = []

    def t(px: float, py: float, s: str, size: int = 14, fill: str = "fg", weight: int = 400,
          anchor: str = "start") -> None:
        parts.append(f'<text x="{px}" y="{py}" font-size="{size}" font-weight="{weight}" '
                     f'fill="{c.get(fill, fill)}" text-anchor="{anchor}">{escape(s)}</text>')

    def box(px: float, py: float, w: float, h: float, stroke: str = "line") -> None:
        parts.append(f'<rect x="{px}" y="{py}" width="{w}" height="{h}" rx="12" '
                     f'fill="{c["card"]}" stroke="{c[stroke]}" stroke-width="1.5"/>')

    def arrow(x1: float, y1: float, x2: float, y2: float) -> None:
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{c["sub"]}" '
                     f'stroke-width="2" marker-end="url(#arr)"/>')

    # ① 出题
    box(20, 40, 210, 250, "blue")
    t(40, 72, x["s1"], 18, "blue", 700)
    t(40, 108, x["s1a"])
    t(40, 128, x["s1b"])
    t(40, 166, x["s1c"])
    t(40, 186, x["s1d"])
    t(40, 224, x["s1e"], 13, "sub")
    t(40, 243, x["s1f"], 13, "sub")
    arrow(232, 165, 262, 165)
    # ② 封存
    box(266, 40, 170, 250, "purple")
    t(286, 72, x["s2"], 18, "purple", 700)
    parts.append(f'<rect x="286" y="96" width="130" height="56" rx="8" fill="none" '
                 f'stroke="{c["purple"]}" stroke-dasharray="4 3"/>')
    t(351, 121, "🔒 test_*.py", 14, "fg", 600, "middle")
    t(351, 141, "sha256: 1c49…", 12, "sub", 400, "middle")
    t(286, 182, x["s2a"], 13)
    t(286, 201, x["s2b"], 13, "sub")
    t(286, 236, x["s2c"], 13)
    t(286, 255, x["s2d"], 13)
    arrow(438, 165, 468, 165)
    # ③ 阅卷
    box(472, 40, 330, 250, "green")
    t(492, 72, x["s3"], 18, "green", 700)
    t(492, 98, x["s3a"], 12, "sub")
    t(492, 116, x["s3b"], 12, "sub")
    for i, (label, key) in enumerate((("1", "l1"), ("2", "l2"), ("3", "l3"))):
        y = 136 + i * 42
        parts.append(f'<rect x="492" y="{y}" width="290" height="34" rx="8" fill="{c["bg"]}" '
                     f'stroke="{c["line"]}"/>')
        parts.append(f'<circle cx="510" cy="{y + 17}" r="10" fill="{c["green"]}"/>')
        t(510, y + 22, label, 12, "#ffffff", 700, "middle")
        t(528, y + 22, x[key], 13)
    t(492, 278, x["extra"], 12, "amber", 600)
    arrow(804, 165, 834, 165)
    # 结论
    box(838, 40, 142, 250)
    t(909, 72, x["v"], 18, "fg", 700, "middle")
    for i, (key, col) in enumerate((("va", "green"), ("vb", "red"), ("vc", "amber"))):
        y = 92 + i * 44
        parts.append(f'<rect x="849" y="{y}" width="120" height="32" rx="16" fill="{c[col]}"/>')
        t(909, y + 21, x[key], 13, "#ffffff", 700, "middle")
    t(909, 248, x["vd"], 12, "sub", 400, "middle")
    t(909, 266, x["ve"], 12, "sub", 400, "middle")
    # 驳回回传
    parts.append(f'<path d="M909 296 C909 330, 640 330, 640 296" fill="none" stroke="{c["red"]}" '
                 f'stroke-width="1.6" stroke-dasharray="5 4" marker-end="url(#arrr)"/>')
    t(774, 342, x["loop"], 12, "red", 400, "middle")
    # 底部
    for i, (k, v) in enumerate((("foot1", "foot1a"), ("foot2", "foot2a"))):
        y = 366 + i * 40
        parts.append(f'<rect x="20" y="{y}" width="960" height="32" rx="8" fill="{c["card"]}" '
                     f'stroke="{c["line"]}"/>')
        t(40, y + 21, x[k], 13, "sub", 700)
        t(160, y + 21, x[v], 13)

    defs = (f'<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            f'markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="{c["sub"]}"/></marker>'
            f'<marker id="arrr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            f'markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="{c["red"]}"/>'
            f'</marker></defs>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="452" '
            f'viewBox="0 0 1000 452" {FONT} role="img" aria-label="How FailGate works">'
            f'{defs}<rect width="1000" height="452" rx="16" fill="{c["bg"]}"/>'
            + "".join(parts) + "</svg>\n")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for lang in TEXT:
        for theme in THEMES:
            p = OUT / f"how-it-works.{lang}.{theme}.svg"
            p.write_text(svg(lang, theme), encoding="utf-8")
            print(p)


if __name__ == "__main__":
    main()
