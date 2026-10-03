"""README 素材用：处理 record_cast.py 录下的 asciicast，再交给 agg 渲染。

- 打码：smee 通道地址换成占位符（通道地址能读到 webhook 内容）；
- 剪尾：某段文字第一次出现后再留几秒，后面的丢掉；
- 拼接：多段录制首尾相接，中间插一行灰色说明（例如"中间重启过 up"），不假装是一次录完的；
- 快进等待：两次输出之间的空档超过 cap 秒就压到 cap 秒；--thin-board 丢掉内容没变、只有时钟在走
  的状态板刷新帧。状态板上的运行时钟仍是真实时间，所以画面上能看出哪里快进了。

    python scripts/media/cast_tools.py --out loop.cast --cap 0.35 \\
        --cut "PR_OPENED → CLOSED+10" a.cast "--- 2 min later: /failgate fix ---" b.cast
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
SECRETS = [(re.compile(r"https://smee\.io/[A-Za-z0-9]+"), "https://smee.io/<channel>")]


def load(path: Path) -> tuple[dict, list[list]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0]), [json.loads(x) for x in lines[1:] if x.strip()]


def mask(data: str) -> str:
    for pat, rep in SECRETS:
        data = pat.sub(rep, data)
    return data


def cut_after(events: list[list], text: str, keep: float) -> list[list]:
    seen = ""
    for e in events:
        seen += ANSI.sub("", e[2])
        if text in seen:
            end = e[0] + keep
            return [x for x in events if x[0] <= end]
    return events


CLOCKS = re.compile(r"up \d+:\d\d:\d\d|\d+:\d\d|elapsed \S+")


def thin_board(events: list[list], marker: str = "FailGate live") -> list[list]:
    """`failgate up` 的状态板每 2 秒重画一次；内容（去掉时钟）没变的重画帧丢掉。"""
    out, last = [], None
    for e in events:
        if marker in e[2]:
            sig = CLOCKS.sub("", ANSI.sub("", e[2]))
            if sig == last:
                continue
            last = sig
        out.append(e)
    return out


def compress(events: list[list], cap: float) -> list[list]:
    out, last_src, t = [], 0.0, 0.0
    for e in events:
        t += min(e[0] - last_src, cap)
        last_src = e[0]
        out.append([round(t, 3), e[1], e[2]])
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--cap", type=float, default=0.0, help="空档最长保留多少秒（0 = 不压）")
    p.add_argument("--thin-board", action="store_true", help="丢掉只有时钟在变的状态板重画")
    p.add_argument("--cut", action="append", default=[], metavar="TEXT+SECONDS",
                   help="每段录制：TEXT 第一次出现后再留 SECONDS 秒（对所有段生效）")
    p.add_argument("parts", nargs="+", help="cast 文件，或者要插入的一行说明文字")
    a = p.parse_args()

    header: dict | None = None
    merged: list[list] = []
    t_end = 0.0
    for part in a.parts:
        path = Path(part)
        if path.suffix == ".cast" and path.is_file():
            h, ev = load(path)
            header = header or h
            for spec in a.cut:
                text, _, sec = spec.rpartition("+")
                ev = cut_after(ev, text, float(sec))
            ev = [[e[0], e[1], mask(e[2])] for e in ev]
            if a.thin_board:
                ev = thin_board(ev)
            if a.cap:
                ev = compress(ev, a.cap)
            merged += [[round(t_end + e[0], 3), e[1], e[2]] for e in ev]
            t_end = merged[-1][0] + 1.0
        else:  # 两段之间的说明：清屏 + 一行灰字，停 2.5 秒
            merged.append([round(t_end, 3), "o", f"\x1b[2J\x1b[H\r\n  \x1b[2;3m{part}\x1b[0m\r\n"])
            t_end += 2.5
            merged.append([round(t_end, 3), "o", "\x1b[2J\x1b[H"])
            t_end += 0.2
    assert header is not None, "至少要一个 .cast 文件"
    with a.out.open("w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")
        for e in merged:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"{a.out}: {len(merged)} events, {merged[-1][0]:.1f}s")


if __name__ == "__main__":
    main()
