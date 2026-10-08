from inbox.models import Playbook


def test_playbook_editor_saves(admin_client, db):
    page = admin_client.get("/playbook/")
    assert page.status_code == 200 and b"Signature" in page.content
    response = admin_client.post("/playbook/", {"company_name": "Indus Freight", "signature": "Thanks,\nOps",
                                                "tone": "Warm", "facts": "Open 9-6", "never_promise": "No prices"})
    assert response.status_code == 302
    playbook = Playbook.get()
    assert playbook.signature == "Thanks,\nOps" and playbook.updated_by == "admin"


def test_playbook_needs_login(client, db):
    assert client.get("/playbook/").status_code == 302


def test_new_drafts_use_the_playbook(admin_client, api, fake_llm):
    from .test_m4_draft import _triaged

    admin_client.post("/playbook/", {"company_name": "Indus Freight", "signature": "Cheers,\nDesk 7",
                                     "tone": "Warm", "facts": "", "never_promise": ""})
    email_id, llm = _triaged(api, fake_llm, draft="Hi Sarah, thanks.")
    body = api("post", f"/emails/{email_id}/draft").json()
    assert "Cheers,\nDesk 7" in llm.calls[-1]["system"]
    assert body["body"].endswith("Cheers,\nDesk 7")  # appended by code


def test_detail_shows_draft_and_decision(admin_client, api, fake_llm):
    from .test_m4_draft import _awaiting

    email_id = _awaiting(api, fake_llm)
    api("post", f"/emails/{email_id}/approval", {"decision": "approved", "reviewer": "slack"})
    page = admin_client.get(f"/emails/{email_id}/").content.decode()
    assert "Thanks for your request" in page and "approved" in page
