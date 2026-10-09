from inbox import process
from inbox.models import Email

from .test_m1_intake import PAYLOAD

QUOTE = {**PAYLOAD, "gmail_message_id": "m-quote",
         "text": "Acme Textiles here. Need 4 pallets Lahore to Karachi next Tuesday."}
CLAIM = {**PAYLOAD, "gmail_message_id": "m-claim", "subject": "Damaged SH-1022",
         "text": "Cargo arrived damaged, 2 cartons crushed. Ref SH-1022. Photos attached."}


def _triaged(api, fake_llm, payload=None, **llm):
    fake_llm(**llm)
    email_id = api("post", "/emails", payload or PAYLOAD).json()["id"]
    return api("post", f"/emails/{email_id}/triage").json()


def test_quote_payload_for_hubspot(api, fake_llm):
    body = _triaged(api, fake_llm, QUOTE, fields={"origin_city": "Lahore", "destination_city": "Karachi", "pieces": 4,
                                                   "company": "Acme Textiles"}, evidence={"pieces": "4 pallets"})
    crm = body["crm"]
    # identity comes from the Gmail header, not the LLM
    assert crm["contact"] == {"email": "sarah@acme-textiles.example", "firstname": "Sarah", "lastname": "Khan"}
    assert crm["deal_name"] == "Acme Textiles Lahore→Karachi"
    assert crm["deal_stage"] == "info_requested"  # pickup_date missing
    assert "<b>Origin:</b> Lahore" in crm["note_body"]
    assert "Pickup date" in crm["note_body"]  # listed as missing


def test_complete_quote_is_new_request(api, fake_llm):
    body = _triaged(api, fake_llm, QUOTE, fields={"origin_city": "Lahore", "destination_city": "Karachi", "pieces": 4,
                                                   "pickup_date": "2026-10-13"},
                    evidence={"pickup_date": "next Tuesday", "pieces": "4 pallets"})
    assert body["missing_fields"] == [] and body["crm"]["deal_stage"] == "new_request"


def test_deal_name_falls_back_to_sender(db):
    email = Email(from_email="a@b.example", from_name="", subject="x")
    assert process.deal_name(email, {}) == "b.example ?→?"


def test_note_escapes_html(api, fake_llm):
    payload = {**PAYLOAD, "gmail_message_id": "m-x", "text": "<script>x</script> from Lahore"}
    body = _triaged(api, fake_llm, payload, fields={"origin_city": "<script>x</script>"},
                    evidence={"origin_city": "<script>x</script>"})
    assert "<script>" not in body["crm"]["note_body"]


def test_lookup_refs_dedup_and_order():
    refs = process.lookup_refs({"container_numbers": ["MSCU 1234566", "MSCU1234566"], "bl_numbers": ["KHI-1"],
                                "po_numbers": ["7781"], "booking_reference": "BK-9"})
    assert refs == [{"kind": "container", "ref": "MSCU1234566"}, {"kind": "bl", "ref": "KHI-1"},
                    {"kind": "po", "ref": "7781"}, {"kind": "booking", "ref": "BK-9"}]


def test_upload_attachments_filters_types():
    email = Email(attachments=[
        {"filename": "INV.PDF", "gmail_attachment_id": "a"},
        {"filename": "list.xlsx", "gmail_attachment_id": "b"},
        {"filename": "virus.exe", "gmail_attachment_id": "c"},
        {"filename": "nolink.pdf"},
    ])
    assert [a["gmail_attachment_id"] for a in process.upload_attachments(email)] == ["a", "b"]


def test_claim_slack_text(api, fake_llm):
    body = _triaged(api, fake_llm, CLAIM, classification={"category": "claim"},
                    fields={"reference_numbers": ["SH-1022"], "pieces_affected": 2, "photos_attached": True},
                    evidence={"reference_numbers": "SH-1022", "pieces_affected": "2 cartons", "photos_attached": "Photos attached"})
    text = body["slack"]["claim_text"]
    assert text.startswith(":rotating_light: *Claim*")
    assert "References: SH-1022" in text and "Pieces affected: 2" in text
    assert body["urgency"] == "high"


def test_slack_text_escapes_markup(db):
    email = Email(id=5, from_email="a@b.example", from_name="<!channel>", subject="x & <y>")
    text = process.review_text(email, "because")
    assert "<!channel>" not in text and "&lt;!channel&gt;" in text
    assert "x &amp; &lt;y&gt;" in text


def test_shipmatch_flag_in_payload(api, fake_llm, settings):
    settings.SHIPMATCH_ENABLED = True
    assert _triaged(api, fake_llm)["shipmatch_enabled"] is True


def test_get_email_returns_same_payload(api, fake_llm):
    body = _triaged(api, fake_llm)
    again = api("get", f"/emails/{body['id']}").json()
    assert again["crm"] == body["crm"] and again["route"] == body["route"]
