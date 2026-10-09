from inbox.cleaning import clean_body


def test_gmail_quote_removed():
    body = "Thanks, pickup Tuesday works.\n\nOn Mon, Oct 5, 2026 at 3:12 PM Ali <ali@x.example> wrote:\n> Can you pick up Tue?\n> Thanks"
    assert clean_body(body) == "Thanks, pickup Tuesday works."


def test_gmail_quote_header_wrapped_over_two_lines():
    body = "Ok confirmed.\n\nOn Mon, Oct 5, 2026 at 3:12 PM Ali Raza <ali@x.example>\nwrote:\n> earlier"
    assert clean_body(body) == "Ok confirmed."


def test_outlook_header_block_removed():
    body = "Please see below.\n\nFrom: Ops Team <ops@indusfreight.example>\nSent: Monday, October 5, 2026 3:00 PM\nTo: Ali\nSubject: RE: rate\n\nOld text"
    assert clean_body(body) == "Please see below."


def test_original_message_marker():
    assert clean_body("Yes.\n-----Original Message-----\nold") == "Yes."


def test_signature_removed():
    body = "Need a rate Lahore to Karachi.\n\n-- \nAli Raza\nLogistics Manager\n+92 300 0000000"
    assert clean_body(body) == "Need a rate Lahore to Karachi."


def test_sent_from_iphone_removed():
    assert clean_body("Where is my container?\n\nSent from my iPhone") == "Where is my container?"


def test_forwarded_content_kept():
    body = "FYI see below, please handle.\n\n---------- Forwarded message ---------\nFrom: Client <c@y.example>\nDate: Mon, Oct 5\n\nNeed 2 trucks to Multan."
    cleaned = clean_body(body)
    assert "Need 2 trucks to Multan." in cleaned
    assert cleaned.startswith("FYI see below")


def test_inline_quote_lines_dropped():
    assert clean_body("> old line\nNew answer") == "New answer"
