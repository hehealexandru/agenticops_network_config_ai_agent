import json

from llm_provider import serialize_tool_result

LIMIT = 16000


def test_small_result_is_unchanged():
    result = {"status": "success", "output": "R1#show ip interface brief\n..."}
    assert serialize_tool_result(result, LIMIT) == json.dumps(result, ensure_ascii=False)


def test_long_output_stays_valid_json():
    result = {"status": "success", "host": "10.0.100.1", "output": "x" * 50000}
    text = serialize_tool_result(result, LIMIT)
    data = json.loads(text)  # the old code produced invalid JSON here
    assert len(text) <= LIMIT
    assert data["status"] == "success"
    assert data["host"] == "10.0.100.1"
    assert data["truncated"] is True
    assert "chars omitted" in data["output"]


def test_nested_device_info_is_shortened():
    # collect_device_info returns several long fields inside "info".
    result = {"status": "success", "host": "10.0.100.1", "info": {
        "interfaces": "i" * 3000,
        "routes": "r" * 8000,
        "running_config": "c" * 30000,
    }}
    text = serialize_tool_result(result, LIMIT)
    data = json.loads(text)
    assert len(text) <= LIMIT
    assert set(data["info"]) == {"interfaces", "routes", "running_config"}


def test_error_status_is_preserved():
    result = {"status": "error", "error": "Authentication failed", "output": "e" * 40000}
    data = json.loads(serialize_tool_result(result, LIMIT))
    assert data["status"] == "error"
    assert data["error"] == "Authentication failed"


def test_many_small_values_fall_back_to_preview():
    result = {"status": "success", "items": [{"n": i} for i in range(5000)]}
    text = serialize_tool_result(result, LIMIT)
    data = json.loads(text)
    assert len(text) <= LIMIT
    assert data["truncated"] is True
    assert "preview" in data


def test_unicode_output():
    result = {"status": "success", "output": "ș" * 40000}
    text = serialize_tool_result(result, LIMIT)
    assert len(text) <= LIMIT
    assert json.loads(text)["status"] == "success"