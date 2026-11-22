"""OpenAPI 契约：分页信封与错误信封必须出现在文档里。"""


def test_openapi_documents_pagination_and_error_envelopes(client):
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]

    assert "ErrorEnvelope" in schemas
    assert "ErrorDetail" in schemas
    assert "Page_AgentRead_" in schemas
    assert "Page_ConversationRead_" in schemas
    assert "Page_MessageRead_" in schemas


def test_send_message_documents_model_errors(client):
    spec = client.get("/openapi.json").json()
    operation = spec["paths"]["/api/v1/conversations/{conversation_id}/messages"]["post"]

    assert {"404", "422", "424", "502", "504"} <= set(operation["responses"])


def test_send_message_documents_run_id(client):
    spec = client.get("/openapi.json").json()
    response = spec["components"]["schemas"]["SendMessageResponse"]
    assert "run_id" in response["properties"]
