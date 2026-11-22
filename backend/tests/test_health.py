

def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_sets_request_id_header(client):
    response = client.get("/health")
    assert response.headers["X-Request-ID"].startswith("req_")


def test_client_supplied_request_id_is_echoed(client):
    response = client.get("/health", headers={"X-Request-ID": "trace-from-client"})
    assert response.headers["X-Request-ID"] == "trace-from-client"
