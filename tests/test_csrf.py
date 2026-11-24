def test_signed_in_form_without_token_revokes_session(client, signed_in):
    signed_in(client)
    response = client.post("/auth/logout", data={"unused": "value"})
    assert response.status_code == 400
    assert response.json()["code"] == 40002
    response = client.get("/members")
    assert response.status_code == 401
    assert response.json()["code"] == 40101


def test_form_uses_the_hidden_token_not_the_header(client, signed_in):
    csrf = signed_in(client)
    response = client.post("/auth/logout", data={"unused": "value"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 400
    assert response.json()["code"] == 40002


def test_anon_mismatch_does_not_revoke_session(client, signed_in, application_form):
    signed_in(client)
    data = application_form() | {"csrf_token": "mismatched"}
    response = client.post("/apply", data=data)
    assert response.status_code == 400
    assert response.json()["code"] == 40002
    assert client.get("/members").status_code == 200


def test_anon_matching_double_submit_shows_receipt(client, application_form):
    response = client.post("/apply", data=application_form())
    assert response.status_code == 200
    assert 'id="receipt-code"' in response.text
