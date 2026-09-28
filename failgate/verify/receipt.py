"""证据收据（技术方案 9.4 节）：复现结论的规范化、可复验记录。

一份收据说清楚"在什么代码、什么环境、用什么命令、跑出了什么"，并给出考卷（L2 测试）的
sha256。之后 ClaimVerify 核验 PR 时永远跑收据里封存的那份考卷；PR 里的同名文件只拿来
比对哈希，作为篡改信号。

哈希规则（写进收据的 schema 版本，改规则就升版本）：
- 代码：换行统一成 LF（\\r\\n、\\r → \\n）后按 UTF-8 算 sha256。Windows 上编辑、git 的
  autocrlf 都会改换行符，这不算篡改；
- 收据：去掉 receipt_sha256 字段本身，JSON 键排序、不转义非 ASCII、紧凑分隔符，按 UTF-8
  算 sha256。任何人拿到收据 JSON 都能独立重算。

不做密码学签名（Sigstore 等）：自托管场景下，收据的可信来自"可以复验"，而不是"签了名"。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from failgate import __version__
from failgate.index.trace import TraceSignature
from failgate.repro.judge import RunRecord, Verdict

SCHEMA: Literal["failgate.receipt/v1"] = "failgate.receipt/v1"


def normalize_code(code: str) -> str:
    return code.replace("\r\n", "\n").replace("\r", "\n")


def code_sha256(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode("utf-8")).hexdigest()


class EvidenceReceipt(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # BaseModel 自己有 schema() 方法，字段名避开它，序列化时用别名 "schema"
    schema_: Literal["failgate.receipt/v1"] = Field(SCHEMA, alias="schema")
    evidence_id: str
    repo: str
    issue: int
    level: str  # L1 / L2
    mode: str  # package / source
    # 能不能当考卷：只有 L2（仓库内的测试）可以。L1 是独立脚本，维护者没法直接合进仓库
    acceptance: bool
    test_path: str
    test_sha256: str
    package: str
    version: str | None = None  # package 模式：装的发布版；source 模式：伪版本号
    source_repo: str | None = None
    source_sha: str | None = None
    python: str | None = None
    pytest: str | None = None
    command: list[str]  # 沙箱里实际执行的命令
    signature: TraceSignature | None = None  # 观察到的失败签名
    runs: list[RunRecord]
    verdict: str
    score: float | None = None
    score_method: str | None = None  # signature（签名比对）/ llm（没有堆栈时的 LLM 评委）
    failgate_version: str
    created_at: str  # UTC，ISO 8601，以 Z 结尾

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceReceipt:
        return cls.model_validate({k: v for k, v in data.items() if k != "receipt_sha256"})

    def digest(self) -> str:
        return receipt_digest(self.to_dict())

    def signed_dict(self) -> dict[str, Any]:
        """收据 + 自己的哈希：写进数据库和评论的就是这个。"""
        data = self.to_dict()
        data["receipt_sha256"] = receipt_digest(data)
        return data


class SealedTest(BaseModel):
    """要封存的一份证据：收据，加上它哈希的那份完整代码（评论里的代码可能被截断）。"""

    receipt: EvidenceReceipt
    code: str

    def signed(self) -> dict[str, Any]:
        return self.receipt.signed_dict()


def canonical_json(data: dict[str, Any]) -> str:
    body = {k: v for k, v in data.items() if k != "receipt_sha256"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def receipt_digest(data: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def check_receipt(data: dict[str, Any], code: str | None = None) -> list[str]:
    """重算哈希，返回不一致的地方（空列表 = 一致）。code 给了就同时核对考卷的哈希。"""
    problems: list[str] = []
    claimed = data.get("receipt_sha256")
    if claimed != receipt_digest(data):
        problems.append("收据内容和 receipt_sha256 对不上：收据被改过")
    if code is not None and code_sha256(code) != data.get("test_sha256"):
        problems.append("测试代码和 test_sha256 对不上：考卷被改过")
    return problems


def build_receipt(
    *,
    repo: str,
    issue: int,
    level: str,
    mode: str,
    test_path: str,
    code: str,
    package: str,
    command: list[str],
    verdict: Verdict,
    version: str | None = None,
    source_repo: str | None = None,
    source_sha: str | None = None,
    python: str | None = None,
    pytest: str | None = None,
    now: datetime | None = None,
    evidence_id: str | None = None,
) -> EvidenceReceipt:
    created = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    return EvidenceReceipt(
        evidence_id=evidence_id or uuid.uuid4().hex,
        repo=repo, issue=issue, level=level, mode=mode, acceptance=level == "L2",
        test_path=test_path, test_sha256=code_sha256(code), package=package, version=version,
        source_repo=source_repo, source_sha=source_sha, python=python, pytest=pytest,
        command=command, signature=verdict.observed, runs=verdict.records,
        verdict=verdict.kind.value, score=verdict.match, score_method=verdict.match_method,
        failgate_version=__version__,
        created_at=created.isoformat().replace("+00:00", "Z"),
    )
