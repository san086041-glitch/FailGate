"""README 素材用：在真实的伪终端里运行一条命令，按真实时间录成 asciicast v2。

VHS 在 Windows 11 上录不了（ttyd 的 ConPTY 问题，charmbracelet/vhs#721），所以本机录制用
pywinpty（Windows 原生伪终端）+ asciinema 的 agg 渲染 GIF。录下来的是命令的原始输出字节和
真实时间；唯一加上去的是开头"打字"的提示符动画。

    pip install pywinpty
    python scripts/media/record_cast.py --out up.cast --type "failgate up" \\
        --trigger-on "转发|forwarding" --trigger "gh issue create ..." \\
        --until "REPRODUCED" --after 6 -- .venv/Scripts/failgate.exe up
    agg --speed 3 up.cast up.gif

--trigger 只在屏幕上第一次出现 --trigger-on 时执行一次（例如服务就绪后开一个真实 issue）；
出现 --until 后再等 --after 秒，发 Ctrl+C 让命令自己正常退出。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from winpty import PtyProcess

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-B]")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--cols", type=int, default=118)
    p.add_argument("--rows", type=int, default=32)
    p.add_argument("--type", dest="typed", default="", help="开头打字动画显示的命令")
    p.add_argument("--trigger-on", default=None)
    p.add_argument("--trigger", default=None, help="看到 --trigger-on 后执行的命令（只执行一次）")
    p.add_argument("--step", action="append", default=[], metavar="REGEX=>CMD",
                   help="按顺序执行：上一步之后的输出里出现 REGEX 时执行 CMD（可重复）")
    p.add_argument("--until", default=None, help="看到这段文字后准备结束")
    p.add_argument("--after", type=float, default=5.0)
    p.add_argument("--max", type=float, default=900.0, help="最长录多少秒")
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    a = p.parse_args()
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    exe = shutil.which(cmd[0]) or cmd[0]
    cmd = [str(Path(exe).resolve()), *cmd[1:]]  # 伪终端要绝对路径

    events: list[list] = []
    t0 = time.monotonic()
    # 开头的提示符和打字动画（只是显示；真正执行的是下面的 cmd）
    clock = 0.4
    events.append([clock, "o", "\x1b[1;34m❯\x1b[0m "])
    for ch in a.typed:
        clock += 0.07
        events.append([round(clock, 3), "o", ch])
    clock += 0.5
    events.append([round(clock, 3), "o", "\r\n"])
    offset = clock

    proc = PtyProcess.spawn(subprocess.list2cmdline(cmd), dimensions=(a.rows, a.cols))
    screen: list[str] = []
    lock = threading.Lock()

    def reader() -> None:
        while True:
            try:
                data = proc.read(4096)
            except EOFError:
                return
            if data:
                with lock:
                    events.append([round(offset + time.monotonic() - t0, 3), "o", data])
                    screen.append(ANSI.sub("", data))

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    steps = [tuple(x.split("=>", 1)) for x in a.step]
    if a.trigger and a.trigger_on:
        steps.insert(0, (a.trigger_on, a.trigger))
    since = 0  # 只在上一步之后的输出里找下一步的触发文字
    done_at = None
    while proc.isalive():
        time.sleep(0.25)
        with lock:
            text = "".join(screen)
        now = time.monotonic() - t0
        recent = text[since:]
        if steps and re.search(steps[0][0], recent):
            pattern, command = steps.pop(0)
            since = len(text)
            print(f"[record] {now:.1f}s saw {pattern!r} -> {command}", flush=True)
            subprocess.Popen(command, shell=True)
        elif not steps and a.until and done_at is None and re.search(a.until, recent):
            done_at = now
            print(f"[record] saw {a.until!r} at {now:.1f}s", flush=True)
        if (done_at is not None and now - done_at >= a.after) or now >= a.max:
            print("[record] sending Ctrl+C", flush=True)
            proc.write("\x03")
            deadline = time.monotonic() + 30
            while proc.isalive() and time.monotonic() < deadline:
                time.sleep(0.25)
            break
    if proc.isalive():
        proc.terminate(force=True)
    th.join(timeout=2)

    header = {"version": 2, "width": a.cols, "height": a.rows, "timestamp": int(time.time()),
              "env": {"TERM": "xterm-256color", "SHELL": "powershell"}}
    with a.out.open("w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")
        for e in sorted(events, key=lambda e: e[0]):
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"[record] {len(events)} events, {events[-1][0]:.1f}s -> {a.out}")


if __name__ == "__main__":
    main()
