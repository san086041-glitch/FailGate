from warden.index.trace import TraceSignature
from warden.repro.signature import failure_signature, last_traceback, match_score, package_path

# 用户报告里的堆栈：装在用户家目录的 site-packages 里，前面还有用户自己的脚本
REPORTED = """\
Traceback (most recent call last):
  File "C:\\Users\\u\\proj\\main.py", line 3, in <module>
    black.format_str(src, mode=black.Mode())
  File "C:\\Users\\u\\.venv\\Lib\\site-packages\\black\\__init__.py", line 1204, in format_str
    dst = _format_str_once(src, mode=mode)
  File "C:\\Users\\u\\.venv\\Lib\\site-packages\\black\\__init__.py", line 1218, in _format_str_once
    for current_line in line_generator.visit(src_node):
  File "C:\\Users\\u\\.venv\\Lib\\site-packages\\black\\linegen.py", line 150, in visit_stmt
    yield from self.line()
  File "C:\\Users\\u\\.venv\\Lib\\site-packages\\black\\linegen.py", line 88, in line
    raise KeyError(key)
KeyError: 'walrus_42'
"""

# 沙箱里复现出来的：source 模式，代码在 /workspace/src 下，行号不同
OBSERVED = """\
Traceback (most recent call last):
  File "/workspace/.warden/repro.py", line 5, in <module>
    black.format_str(SRC, mode=black.Mode())
  File "/workspace/src/black/__init__.py", line 1190, in format_str
  File "/workspace/src/black/__init__.py", line 1203, in _format_str_once
  File "/workspace/src/black/linegen.py", line 151, in visit_stmt
  File "/workspace/src/black/linegen.py", line 90, in line
KeyError: 'walrus_7'
"""


def test_package_path_strips_install_location():
    assert package_path("/usr/lib/python3.12/site-packages/black/linegen.py", "black") == (
        "black/linegen.py"
    )
    assert package_path("C:\\x\\site-packages\\black\\nodes.py", "black") == "black/nodes.py"
    assert package_path("/workspace/src/black/__init__.py", "black") == "black/__init__.py"
    # 单文件模块、包名里的连字符
    assert package_path("/venv/lib/six.py", "six") == "six.py"
    assert package_path("/venv/lib/typing_extensions.py", "typing-extensions") == (
        "typing_extensions.py"
    )
    assert package_path("/usr/lib/python3.12/json/decoder.py", "black") is None


def test_signature_keeps_only_innermost_package_frames():
    sig = failure_signature(REPORTED, "black")
    assert sig is not None
    assert sig.exc_type == "KeyError"
    assert sig.message == "'walrus_<N>'"
    # 用户脚本被丢掉；只留离抛出点最近的 3 层
    assert sig.frames == [
        "black/__init__.py:_format_str_once",
        "black/linegen.py:visit_stmt",
        "black/linegen.py:line",
    ]


def test_same_bug_different_machine_matches_fully():
    a, b = failure_signature(REPORTED, "black"), failure_signature(OBSERVED, "black")
    assert a is not None and b is not None
    assert a.frames == b.frames
    assert match_score(b, a) == 1.0


def test_only_last_exception_in_chain_counts():
    chained = (
        'Traceback (most recent call last):\n  File "/v/site-packages/black/a.py", line 1, in f\n'
        "KeyError: 'x'\n\nDuring handling of the above exception, another exception occurred:\n\n"
        'Traceback (most recent call last):\n  File "/v/site-packages/black/b.py", line 2, in g\n'
        "ValueError: bad\n"
    )
    assert last_traceback(chained).startswith("Traceback")
    sig = failure_signature(chained, "black")
    assert sig is not None and sig.exc_type == "ValueError" and sig.frames == ["black/b.py:g"]


def test_score_weights():
    rep = TraceSignature(exc_type="KeyError", message="'x'", frames=["p/a.py:f", "p/b.py:g"])
    # 类型不同、帧不重合、消息完全不同 → 0
    other = TraceSignature(exc_type="ValueError", message="zzz", frames=["p/c.py:h"])
    assert match_score(other, rep) == 0.0
    # 类型相同、一半的帧重合（Jaccard 1/3）、消息相同 → 0.5 + 0.1 + 0.2
    half = TraceSignature(exc_type="KeyError", message="'x'", frames=["p/a.py:f", "p/c.py:h"])
    assert match_score(half, rep) == 0.8
    # 同类型但抛出位置完全不同：0.5 + 0 + 0.2 = 0.7，仍过 0.6 —— 消息一致时同类型异常大概率是同一个
    elsewhere = TraceSignature(exc_type="KeyError", message="'x'", frames=["p/z.py:q"])
    assert match_score(elsewhere, rep) == 0.7
    assert match_score(None, rep) == 0.0


def test_no_package_frames_on_either_side_falls_back_to_type():
    # 从 C 扩展里直接抛出：两边都没有包内帧，这一项不应该拉低分数
    a = TraceSignature(exc_type="MemoryError", message="", frames=[])
    assert match_score(a, a) == 1.0


def test_exception_names_without_error_suffix():
    # 回放 #4599：black 的 InvalidInput 不以 Error 结尾，以前识别不出异常类型
    out = (
        "Traceback (most recent call last):\n"
        '  File "/workspace/repro.py", line 17, in <module>\n'
        "    formatted = black.format_str(SRC, mode=mode)\n"
        '  File "src/black/__init__.py", line 1204, in format_str\n'
        '  File "src/black/parsing.py", line 92, in lib2to3_parse\n'
        "black.parsing.InvalidInput: Cannot parse for target version Python 3.12: 1:21: x\n"
    )
    sig = failure_signature(out, "black")
    assert sig is not None and sig.exc_type == "InvalidInput"
    assert sig.message.startswith("Cannot parse for target version Python <N>")
    assert failure_signature(out.replace("black.parsing.InvalidInput: Cannot parse for target "
                                         "version Python 3.12: 1:21: x", "black.report."
                                         "NothingChanged"), "black").exc_type == "NothingChanged"
    assert match_score(sig, sig) == 1.0


def test_plain_text_without_traceback():
    assert failure_signature("everything fine", "black") is None
    sig = failure_signature("AssertionError: expected 1, got 2", "black")
    assert sig is not None and sig.exc_type == "AssertionError" and sig.frames == []
