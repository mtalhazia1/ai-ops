import base64
import email as email_lib
from datetime import timedelta
from email import policy

from django.utils import timezone

from inbox.llm import draft as draft_mod
from inbox.models import Action, Approval, Draft, Email

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


PAPERWORK = {**THREADED, "gmail_message_id": "m-paper", "subject": "Invoice INV-2201",
             "attachments": [{"filename": "INV-2201.pdf", "gmail_attachment_id": "a1"},
                             {"filename": "BL.pdf", "gmail_attachment_id": "a2"},
                             {"filename": "PL.pdf", "gmail_attachment_id": "a3"}]}


def _paperwork(api, fake_llm):
    email_id, llm = _triaged(api, fake_llm, PAPERWORK, classification={"category": "paperwork"})

    def log(att, ok, resp, filename=True):
        api("post", f"/emails/{email_id}/actions", {
            "kind": "shipmatch_upload", "idempotency_key": f"shipmatch_upload:{email_id}:{att}", "ok": ok,
            "request": {"filename": f"{att}.pdf"} if filename else {}, "response": resp})

    def claim(att):
        return api("post", f"/emails/{email_id}/actions/claim",
                   {"kind": "shipmatch_upload", "idempotency_key": f"shipmatch_upload:{email_id}:{att}"}).json()

    return email_id, llm, log, claim


def test_rejected_upload_goes_to_review_instead_of_draft(api, fake_llm):
    email_id, llm, log, _ = _paperwork(api, fake_llm)
    log("a1", True, {"status": 201, "document_id": "doc_1"})
    log("a2", False, {"status": 402, "detail": "plan paused"})
    calls = len(llm.calls)
    body = api("post", f"/emails/{email_id}/draft").json()
    assert body["ok"] is False and body["status"] == "needs_review"
    assert body["problems"] == "ShipMatch did not accept: a2.pdf: uploads paused on the ShipMatch plan (plan paused)"
    assert "Needs review" in body["review_text"] and "a2.pdf" in body["review_text"]
    assert len(llm.calls) == calls  # no AI draft written
    email = Email.objects.get(pk=email_id)
    assert email.needs_review_reason == body["problems"]
    draft = email.latest_draft  # kept as a blocked draft, so nothing can approve "documents received"
    assert draft.ok is False and draft.body == "" and draft.blocked_reason == body["problems"]


def test_rejected_upload_rerun_gives_same_answer(api, fake_llm):
    """A WF2 rerun gets the same {ok:false} answer, not 409, so its review branch runs."""
    email_id, llm, log, _ = _paperwork(api, fake_llm)
    log("a2", False, {"status": 400, "detail": "unsupported"})
    first = api("post", f"/emails/{email_id}/draft").json()
    calls = len(llm.calls)
    again = api("post", f"/emails/{email_id}/draft")
    assert again.status_code == 200
    again = again.json()
    assert again["rerun"] is True and again["ok"] is False and again["status"] == "needs_review"
    assert again["problems"] == first["problems"] and again["review_text"] == first["review_text"]
    assert len(llm.calls) == calls and Draft.objects.filter(email_id=email_id).count() == 1


def test_blocked_draft_rerun_gives_same_answer(api, fake_llm):
    email_id, llm = _triaged(api, fake_llm, draft="Hi,\nPay at https://evil.example/pay now.\nBest regards,\nOps Team")
    first = api("post", f"/emails/{email_id}/draft").json()
    calls = len(llm.calls)
    again = api("post", f"/emails/{email_id}/draft").json()
    assert again["ok"] is False and again["rerun"] is True and again["problems"] == first["problems"]
    assert len(llm.calls) == calls


def test_refusal_text_fallbacks_and_order(api, fake_llm):
    """No "HTTP None" or raw attachment ids; refusals listed in the order they were logged."""
    email_id, _, log, _ = _paperwork(api, fake_llm)
    log("a3", False, {"status": 400}, filename=False)  # name comes from the email's attachment list
    log("zz", False, {"status": 402}, filename=False)  # unknown attachment id
    log("a1", False, {"status": 400, "detail": "not a PDF"})
    body = api("post", f"/emails/{email_id}/draft").json()
    assert body["problems"] == ("ShipMatch did not accept: PL.pdf: file rejected; "
                                "an attachment: uploads paused on the ShipMatch plan; a1.pdf: file rejected (not a PDF)")


def test_successful_uploads_draft(api, fake_llm):
    email_id, _, log, _ = _paperwork(api, fake_llm)
    log("a1", True, {"status": 201})
    log("a2", True, {"status": 200})
    assert api("post", f"/emails/{email_id}/draft").json()["ok"] is True


def test_upload_in_flight_blocks_draft(api, fake_llm):
    """Another run's upload is still in flight: no draft; WF2 stops quietly and the run
    holding the claim drafts once its upload is logged."""
    email_id, llm, log, claim = _paperwork(api, fake_llm)
    log("a1", True, {"status": 201})
    assert claim("a2")["proceed"] is True
    calls = len(llm.calls)
    resp = api("post", f"/emails/{email_id}/draft")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False and body["pending"] is True and body["status"] == "triaged"
    assert "BL.pdf: upload in progress" in body["problems"] and "review_text" not in body
    assert len(llm.calls) == calls and Draft.objects.filter(email_id=email_id).count() == 0
    # the other run's upload is then refused: its own /draft sends the email to review
    log("a2", False, {"status": 402})
    body = api("post", f"/emails/{email_id}/draft").json()
    assert body["ok"] is False and body["status"] == "needs_review" and "a2.pdf" in body["problems"]


def test_transient_upload_failure_fails_instead_of_review(api, fake_llm):
    """429/5xx is not a refusal: /draft answers 503 retry (WF2 fails the run), no review."""
    email_id, llm, log, _ = _paperwork(api, fake_llm)
    log("a1", True, {"status": 201})
    log("a2", False, {"status": 503})
    log("a3", False, {"status": None})
    calls = len(llm.calls)
    resp = api("post", f"/emails/{email_id}/draft")
    assert resp.status_code == 503
    body = resp.json()
    assert body["retry"] is True and body["ok"] is False and "pending" not in body
    assert "a2.pdf: upload failed (HTTP 503)" in body["detail"]
    assert "a3.pdf: upload failed (no response recorded)" in body["detail"]
    email = Email.objects.get(pk=email_id)
    assert email.status == "triaged" and len(llm.calls) == calls and email.drafts.count() == 0


def test_abandoned_upload_claim_fails_instead_of_drafting(api, fake_llm):
    email_id, _, _, claim = _paperwork(api, fake_llm)
    claim("a1")
    old = (timezone.now() - timedelta(minutes=11)).isoformat()
    Action.objects.filter(email_id=email_id).update(response={"pending": True, "claimed_at": old})
    resp = api("post", f"/emails/{email_id}/draft")
    assert resp.status_code == 503 and "INV-2201.pdf: upload never finished" in resp.json()["detail"]
    Action.objects.filter(email_id=email_id).update(response={"released": True})
    resp = api("post", f"/emails/{email_id}/draft")
    assert resp.status_code == 503 and "not repeated after a failed run" in resp.json()["detail"]
