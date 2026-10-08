from inbox.models import Email

PAYLOAD = {
    "gmail_message_id": "msg-1",
    "gmail_thread_id": "thr-1",
    "from": "Sarah Khan <sarah@acme-textiles.example>",
    "to": "ops@indusfreight.example",
    "subject": "rate pls",
    "date": "Thu, 08 Oct 2026 10:00:00 +0500",
    "text": "Need 4 pallets Lahore to Karachi.\n\nOn Wed, 7 Oct 2026 at 09:00, Ops <ops@indusfreight.example> wrote:\n> Hello\n",
    "attachments": [{"filename": "list.pdf", "mime": "application/pdf", "size": 1200, "gmail_attachment_id": "a1"}],
}


def test_insert_then_ignore_duplicates(api):
    first = api("post", "/emails", PAYLOAD)
    assert first.status_code == 200 and first.json()["created"] is True
    for _ in range(2):
        again = api("post", "/emails", PAYLOAD)
        assert again.json() == {"id": first.json()["id"], "created": False, "status": "received"}
    assert Email.objects.count() == 1


def test_three_emails_three_rows(api):
    for i in range(3):
        api("post", "/emails", {**PAYLOAD, "gmail_message_id": f"m{i}"})
    assert Email.objects.count() == 3


def test_parses_headers_and_cleans_body(api):
    api("post", "/emails", PAYLOAD)
    email = Email.objects.get()
    assert email.from_email == "sarah@acme-textiles.example"
    assert email.from_name == "Sarah Khan"
    assert email.to_email == "ops@indusfreight.example"
    assert email.received_at.isoformat() == "2026-10-08T05:00:00+00:00"
    assert email.body_clean == "Need 4 pallets Lahore to Karachi."
    assert "wrote:" in email.body_text
    assert email.attachments[0]["filename"] == "list.pdf"


def test_html_only_email(api):
    api("post", "/emails", {**PAYLOAD, "text": "", "html": "<p>Hello<br>Need a truck</p>"})
    assert Email.objects.get().body_clean == "Hello\nNeed a truck"


def test_intake_requires_token(api):
    assert api("post", "/emails", PAYLOAD, token=None).status_code == 401
