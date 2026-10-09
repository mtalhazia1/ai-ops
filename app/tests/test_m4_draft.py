import base64
import email as email_lib
from email import policy

from inbox.llm import draft as draft_mod
from inbox.models import Approval, Draft, Email

from .test_m1_intake import PAYLOAD

THREADED = {**PAYLOAD, "message_id": "<orig-123@acme.example>", "references": "<older-1@acme.example>",
            "text": "Need 4 pallets Lahore to Karachi, see https://acme-textiles.example/specs"}


def _triaged(api, fake_llm, payload=THREADED, **llm):
    llm_obj = fake_llm(**llm)
    email_id = api("post", "/emails", payload).json()["id"]
    api("post", f"/emails/{email_id}/triage")
    return email_id, llm_obj


def test_draft_ok_moves_to_awaiting_approval(api, fake_llm):
    email_id, llm = _triaged(api, fake_llm)
    body = api("post", f"/emails/{email_id}/draft").json()
    assert body["ok"] is True and body["status"] == "awaiting_approval"
    assert body["subject"] == "Re: rate pls"
    assert "*Approval needed*" in body["approval_text"] and "> Thanks for your request." in body["approval_text"]
    assert Draft.objects.get(email_id=email_id).input_tokens == 300
    # the drafter sees structured data, never the raw email body
    draft_call = llm.calls[-1]
    assert "Lahore to Karachi, see" not in draft_call["user"]
    assert "Indus Freight" in draft_call["system"]


def test_draft_rerun_returns_saved_draft(api, fake_llm):
    email_id, llm = _triaged(api, fake_llm)
    first = api("post", f"/emails/{email_id}/draft").json()
    calls = len(llm.calls)
    again = api("post", f"/emails/{email_id}/draft").json()
    assert again["rerun"] is True and again["body"] == first["body"]
    assert len(llm.calls) == calls and Draft.objects.count() == 1


def test_draft_with_invented_url_is_blocked(api, fake_llm):
    email_id, _ = _triaged(api, fake_llm, draft="Hi,\nPay at https://evil.example/pay now.\nBest regards,\nOps Team")
    body = api("post", f"/emails/{email_id}/draft").json()
    assert body["ok"] is False and body["status"] == "needs_review"
    assert "new link: https://evil.example/pay" in body["problems"]
    assert "draft blocked" in Email.objects.get(pk=email_id).needs_review_reason
    assert "Needs review" in body["review_text"]


def test_draft_may_repeat_links_from_the_email(api, fake_llm):
    email_id, _ = _triaged(api, fake_llm, draft="Hi Sarah,\nWe saw https://acme-textiles.example/specs.\nBest regards,\nOps Team")
    assert api("post", f"/emails/{email_id}/draft").json()["ok"] is True


def test_draft_with_price_is_blocked(api, fake_llm):
    email_id, _ = _triaged(api, fake_llm, draft="Hi Sarah, the rate is PKR 45,000.\nBest regards,\nOps Team")
    assert "contains a money amount" in api("post", f"/emails/{email_id}/draft").json()["problems"]


def test_no_auto_draft_for_emails_in_review(api, fake_llm):
    email_id, _ = _triaged(api, fake_llm, classification={"category": "other"})
    assert api("post", f"/emails/{email_id}/draft").status_code == 409


def test_signature_is_appended_when_missing():
    playbook = draft_mod.PlaybookData("Indus Freight", "Best regards,\nOps Team\nIndus Freight")
    assert draft_mod._ensure_signature("Hi", playbook.signature).endswith("Ops Team\nIndus Freight")
    assert draft_mod._ensure_signature("Hi\n\nBest regards,\nOps", playbook.signature) == "Hi\n\nBest regards,\nOps"


def test_reply_subject():
    assert draft_mod.reply_subject("rate pls") == "Re: rate pls"
    assert draft_mod.reply_subject("RE: rate pls") == "RE: rate pls"
    assert draft_mod.reply_subject("") == "Re: your email"


def test_lookups_reach_the_drafter(api, fake_llm, settings):
    settings.SHIPMATCH_ENABLED = True
    email_id, llm = _triaged(api, fake_llm, classification={"category": "shipment_status"})
    api("post", f"/emails/{email_id}/actions", {"kind": "shipmatch_lookup", "idempotency_key": f"lk:{email_id}",
                                                 "response": {"results": [{"ref": "MSCU1234566", "found": True,
                                                                           "matches": [{"status": "ready"}]}]}})
    api("post", f"/emails/{email_id}/draft")
    assert '"status": "ready"' in llm.calls[-1]["user"]


def _decode(raw):
    return email_lib.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default)


def _awaiting(api, fake_llm):
    email_id, _ = _triaged(api, fake_llm)
    api("post", f"/emails/{email_id}/draft")
    return email_id


def test_approve_builds_threaded_reply(api, fake_llm):
    email_id = _awaiting(api, fake_llm)
    body = api("post", f"/emails/{email_id}/approval", {"decision": "approved", "reviewer": "slack"}).json()
    assert body["status"] == "approved" and body["decision"] == "approved" and body["rerun"] is False
    msg = _decode(body["raw"])
    assert msg["To"] == "Sarah Khan <sarah@acme-textiles.example>"  # from Gmail metadata
    assert msg["Subject"] == "Re: rate pls"
    assert msg["In-Reply-To"] == "<orig-123@acme.example>"
    assert msg["References"] == "<older-1@acme.example> <orig-123@acme.example>"
    assert "Thanks for your request" in msg.get_content()
    assert body["gmail_thread_id"] == "thr-1"


def test_double_click_returns_same_decision(api, fake_llm):
    email_id = _awaiting(api, fake_llm)
    api("post", f"/emails/{email_id}/approval", {"decision": "approved"})
    again = api("post", f"/emails/{email_id}/approval", {"decision": "approved"})
    assert again.status_code == 200 and again.json()["rerun"] is True
    assert Approval.objects.count() == 1
    # a different decision after the fact is refused
    assert api("post", f"/emails/{email_id}/approval", {"decision": "rejected"}).status_code == 409


def test_edited_body_is_sent(api, fake_llm):
    email_id = _awaiting(api, fake_llm)
    body = api("post", f"/emails/{email_id}/approval",
               {"decision": "approved", "channel": "dashboard", "reviewer": "ali", "final_body": "Edited text"}).json()
    assert body["decision"] == "edited" and _decode(body["raw"]).get_content().strip() == "Edited text"


def test_reject(api, fake_llm):
    email_id = _awaiting(api, fake_llm)
    body = api("post", f"/emails/{email_id}/approval", {"decision": "rejected", "reviewer": "slack"}).json()
    assert body["status"] == "rejected" and "raw" not in body
    assert api("post", f"/emails/{email_id}/approval", {"decision": "rejected"}).json()["rerun"] is True
    assert api("post", f"/emails/{email_id}/approval", {"decision": "approved"}).status_code == 409


def test_approval_needs_awaiting_or_review(api, fake_llm):
    email_id, _ = _triaged(api, fake_llm)  # triaged, no draft yet
    assert api("post", f"/emails/{email_id}/approval", {"decision": "approved"}).status_code == 409


def test_review_email_needs_final_body(api, fake_llm):
    email_id, _ = _triaged(api, fake_llm, classification={"category": "other"})
    assert api("post", f"/emails/{email_id}/approval", {"decision": "approved", "channel": "dashboard"}).status_code == 422
    ok = api("post", f"/emails/{email_id}/approval", {"decision": "approved", "channel": "dashboard",
                                                       "final_body": "Thanks, noted."})
    assert ok.status_code == 200 and ok.json()["decision"] == "edited"


def test_bad_decision_rejected(api, fake_llm):
    email_id = _awaiting(api, fake_llm)
    assert api("post", f"/emails/{email_id}/approval", {"decision": "maybe"}).status_code == 422


def test_full_happy_path_states(api, fake_llm):
    email_id = _awaiting(api, fake_llm)
    api("post", f"/emails/{email_id}/approval", {"decision": "approved"})
    assert api("post", f"/emails/{email_id}/status", {"status": "done"}).json()["status"] == "done"
    assert api("post", f"/emails/{email_id}/status", {"status": "approved"}).status_code == 409
    # WF3 retried after the send: same decision is replayed, so it can finish
    assert api("post", f"/emails/{email_id}/approval", {"decision": "approved"}).json()["rerun"] is True


def test_intake_stores_threading_headers(api):
    email_id = api("post", "/emails", {**THREADED, "reply_to": "Desk <desk@acme.example>"}).json()["id"]
    email = Email.objects.get(pk=email_id)
    assert email.rfc_message_id == "<orig-123@acme.example>" and email.reply_to == "desk@acme.example"


def test_reply_goes_to_reply_to(api, fake_llm):
    fake_llm()
    email_id = api("post", "/emails", {**THREADED, "reply_to": "desk@acme.example"}).json()["id"]
    api("post", f"/emails/{email_id}/triage")
    api("post", f"/emails/{email_id}/draft")
    raw = api("post", f"/emails/{email_id}/approval", {"decision": "approved"}).json()["raw"]
    assert _decode(raw)["To"] == "desk@acme.example"


def test_send_failure_resumes_at_sending(api, fake_llm):
    email_id = _awaiting(api, fake_llm)
    api("post", f"/emails/{email_id}/approval", {"decision": "approved"})
    key = f"reply_sent:{email_id}"
    api("post", f"/emails/{email_id}/actions/claim", {"kind": "reply_sent", "idempotency_key": key})
    # WF4 records the Gmail failure: email failed, claim released
    api("post", "/failures", {"workflow": "WF3 Execute", "node": "Gmail send reply", "error": "503", "email_id": email_id})
    retry = api("post", f"/emails/{email_id}/approval", {"decision": "approved"}).json()
    assert retry["status"] == "approved" and retry["rerun"] is True and retry["raw"]
    assert api("post", f"/emails/{email_id}/actions/claim", {"kind": "reply_sent", "idempotency_key": key}).json()["proceed"]


def test_failed_without_decision_is_409(api, fake_llm):
    email_id, _ = _triaged(api, fake_llm)
    api("post", f"/emails/{email_id}/status", {"status": "failed", "reason": "x"})
    assert api("post", f"/emails/{email_id}/approval", {"decision": "approved"}).status_code == 409


def test_rejected_upload_goes_to_review_instead_of_draft(api, fake_llm):
    payload = {**THREADED, "gmail_message_id": "m-paper", "subject": "Invoice INV-2201",
               "attachments": [{"filename": "INV-2201.pdf", "gmail_attachment_id": "a1"},
                               {"filename": "BL.pdf", "gmail_attachment_id": "a2"}]}
    email_id, llm = _triaged(api, fake_llm, payload, classification={"category": "paperwork"})
    log = lambda att, ok, resp: api("post", f"/emails/{email_id}/actions", {  # noqa: E731
        "kind": "shipmatch_upload", "idempotency_key": f"shipmatch_upload:{email_id}:{att}", "ok": ok,
        "request": {"filename": f"{att}.pdf"}, "response": resp})
    log("a1", True, {"status": 201, "document_id": "doc_1"})
    log("a2", False, {"status": 402, "detail": "plan paused"})
    calls = len(llm.calls)
    body = api("post", f"/emails/{email_id}/draft").json()
    assert body["ok"] is False and body["status"] == "needs_review"
    assert "a2.pdf: uploads paused on the ShipMatch plan (plan paused)" in body["problems"]
    assert "Needs review" in body["review_text"]
    assert len(llm.calls) == calls  # no draft written
    assert Email.objects.get(pk=email_id).drafts.count() == 0


def test_successful_or_pending_uploads_still_draft(api, fake_llm):
    email_id, _ = _triaged(api, fake_llm, classification={"category": "paperwork"})
    api("post", f"/emails/{email_id}/actions", {"kind": "shipmatch_upload", "idempotency_key": f"u:{email_id}:1",
                                                 "response": {"status": 200}})
    api("post", f"/emails/{email_id}/actions/claim", {"kind": "shipmatch_upload", "idempotency_key": f"u:{email_id}:2"})
    assert api("post", f"/emails/{email_id}/draft").json()["ok"] is True
