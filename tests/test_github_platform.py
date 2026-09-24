import hashlib
import hmac
import json

from conftest import comment_event, issue_event

from warden.platforms.base import CaseKind
from warden.platforms.github import GitHubPlatform, verify_signature

SECRET = "s3cret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_accepts_valid():
    body = b'{"a":1}'
    assert verify_signature(SECRET, body, _sign(body))


def test_verify_signature_rejects_tampered_wrong_secret_and_missing():
    body = b'{"a":1}'
    assert not verify_signature(SECRET, b'{"a":2}', _sign(body))
    assert not verify_signature(SECRET, body, _sign(body, "other"))
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "sha1=abc")
    # 未配置密钥时一律拒绝，而不是一律放行
    assert not verify_signature("", body, _sign(body, ""))


def _parse(event: str, payload: dict):
    p = GitHubPlatform(SECRET)
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": "d-1"}
    return p.parse_event(headers, json.dumps(payload).encode())


def test_parse_issue_opened():
    ev = _parse("issues", issue_event("opened", 7, association="CONTRIBUTOR"))
    assert ev is not None
    assert ev.name == "issue.opened"
    assert ev.case.kind is CaseKind.ISSUE and ev.case.number == 7
    assert ev.actor.login == "alice" and ev.actor.association == "CONTRIBUTOR"
    assert ev.installation_id == 42


def test_association_only_applies_to_issue_author():
    # 维护者关闭别人的 issue 时，issue.author_association 描述的是作者，不能套到 sender 身上
    ev = _parse("issues", issue_event("closed", sender="maint", association="OWNER"))
    assert ev.actor.login == "maint" and ev.actor.association == "NONE"


def test_parse_comment_on_pull_request_is_pull_case():
    ev = _parse("issue_comment", comment_event("hi", 3, on_pull=True))
    assert ev.name == "comment.created"
    assert ev.case.kind is CaseKind.PULL


def test_bot_detection():
    ev = _parse("issues", issue_event("opened", author="dependabot[bot]", bot=True))
    assert ev.actor.is_bot


def test_unknown_event_ignored():
    assert _parse("star", {"action": "created", "repository": {"full_name": "a/b"}}) is None
