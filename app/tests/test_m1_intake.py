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


def test_date_header_minus_0000_is_utc(api):
    response = api("post", "/emails", {**PAYLOAD, "date": "Thu, 08 Oct 2026 10:00:00 -0000"})
    assert response.status_code == 200
    assert Email.objects.get().received_at.isoformat() == "2026-10-08T10:00:00+00:00"


def test_naive_iso_date_is_utc(api):
    response = api("post", "/emails", {**PAYLOAD, "date": "2026-10-08T10:00:00"})
    assert response.status_code == 200
    assert Email.objects.get().received_at.isoformat() == "2026-10-08T10:00:00+00:00"


def test_unparseable_date_falls_back_to_now(api):
    response = api("post", "/emails", {**PAYLOAD, "date": "not a date"})
    assert response.status_code == 200
    assert Email.objects.get().received_at is not None


def test_several_to_addresses_store_the_first_valid_one(api):
    many = ", ".join(f"Person {i} <person{i}@acme-textiles.example>" for i in range(40))  # > 320 chars
    response = api("post", "/emails", {**PAYLOAD, "to": f"undisclosed-recipients:;, {many}"})
    assert response.status_code == 200
    assert Email.objects.get().to_email == "person0@acme-textiles.example"


def test_unparseable_to_is_stored_empty(api):
    response = api("post", "/emails", {**PAYLOAD, "to": "x" * 400})
    assert response.status_code == 200
    assert Email.objects.get().to_email == ""


def test_reply_to_with_several_addresses_uses_first(api):
    api("post", "/emails", {**PAYLOAD, "reply_to": "Desk <desk@acme-textiles.example>, b@acme-textiles.example"})
    assert Email.objects.get().reply_to == "desk@acme-textiles.example"


def test_garbage_reply_to_is_ignored(api):
    api("post", "/emails", {**PAYLOAD, "reply_to": "not an address"})
    assert Email.objects.get().reply_to == ""


def test_unparseable_from_is_saved_without_address(api):
    raw = "Sarah Khan via some broken gateway"
    response = api("post", "/emails", {**PAYLOAD, "from": raw})
    assert response.status_code == 200 and response.json()["created"] is True
    email = Email.objects.get()
    assert email.from_email == ""
    assert email.from_name == raw


def test_long_garbage_from_does_not_500(api):
    response = api("post", "/emails", {**PAYLOAD, "from": "y" * 1000})
    assert response.status_code == 200
    email = Email.objects.get()
    assert email.from_email == "" and len(email.from_name) == 255
