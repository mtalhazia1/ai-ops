def test_health_requires_token(api):
    assert api("get", "/health", token=None).status_code == 401
    assert api("get", "/health", token="wrong").status_code == 401


def test_health_with_token(api):
    response = api("get", "/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_public_liveness_probe(client):
    assert client.get("/healthz").status_code == 200


def test_dashboard_needs_login(client, db):
    assert client.get("/").status_code == 302


def test_dashboard_pages_render(admin_client, api, fake_llm):
    from .test_m1_intake import PAYLOAD

    fake_llm()
    email_id = api("post", "/emails", PAYLOAD).json()["id"]
    api("post", f"/emails/{email_id}/triage")
    assert b"rate pls" in admin_client.get("/emails/").content
    assert b"rate pls" not in admin_client.get("/").content  # triaged: not waiting for a person
    detail = admin_client.get(f"/emails/{email_id}/")
    assert detail.status_code == 200 and b"quote_request" in detail.content
    assert admin_client.get("/evals/").status_code == 200
