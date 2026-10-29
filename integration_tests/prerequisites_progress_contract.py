"""Small published-schema checks shared by source and real HTTP tests.

Only the vocabulary used by the prerequisite result schemas is implemented;
this is deliberately not a general JSON Schema validator.
"""
from datetime import datetime
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "api/openapi.yaml").read_text())
SCHEMAS = CONTRACT["components"]["schemas"]
MAX_BYTES = 64 * 1024**3


def assert_schema(value, schema):
    """Check the schema vocabulary used here, without a validator dependency.

    Unknown validation keywords fail explicitly. Integers match the canonical
    JSON emitted by this API: booleans and fractional numbers are not integers.
    """
    supported = {"$ref", "type", "const", "enum", "required", "properties",
                 "additionalProperties", "minimum", "maximum", "minLength",
                 "maxLength", "pattern", "format", "minItems", "maxItems",
                 "items", "allOf", "if", "then", "else", "description"}
    assert not set(schema) - supported, set(schema) - supported
    if "$ref" in schema:
        assert schema["$ref"].startswith("#/components/schemas/")
        assert_schema(value, SCHEMAS[schema["$ref"].rsplit("/", 1)[1]])
    if "type" in schema:
        declared = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        native = {"object": dict, "array": list, "string": str, "integer": int,
                  "boolean": bool, "null": type(None)}
        assert type(value) in tuple(native[item] for item in declared), (value, declared)
    if "const" in schema:
        assert type(value) is type(schema["const"]) and value == schema["const"]
    if "enum" in schema:
        assert any(type(value) is type(item) and value == item for item in schema["enum"])
    if type(value) is dict:
        assert set(schema.get("required", ())) <= value.keys()
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            assert not value.keys() - properties.keys()
        for key in value.keys() & properties.keys():
            assert_schema(value[key], properties[key])
    if type(value) is list:
        assert schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value))
        for item in value:
            assert_schema(item, schema.get("items", {}))
    if type(value) is int:
        assert schema.get("minimum", value) <= value <= schema.get("maximum", value)
    if type(value) is str:
        assert schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value))
        if "pattern" in schema:
            assert re.search(schema["pattern"], value)
        if "format" in schema:
            assert schema["format"] == "date-time"
            assert datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None
    for branch in schema.get("allOf", ()):
        assert_schema(value, branch)
    if "if" in schema:
        try:
            assert_schema(value, schema["if"])
        except AssertionError:
            branch = "else"
        else:
            branch = "then"
        assert_schema(value, schema.get(branch, {}))


def response_schema(suffix):
    operation = CONTRACT["paths"]["/api/v1/prerequisites/operations/{operation_id}" + suffix]["get"]
    return operation["responses"]["200"]["content"]["application/json"]["schema"]


def assert_binding(progress, plan, snapshot, bundle):
    assert_schema(progress, SCHEMAS["PrerequisiteDownloadProgress"])
    assert progress["plan_digest"] == plan["plan_digest"] == snapshot["plan_digest"]
    component = next(item for item in plan["components"] if item["component_id"] == progress["component_id"])
    artifact = next(item for item in component["artifacts"] if item["artifact_id"] == progress["artifact_id"])
    signed = next(item for item in bundle["descriptor"]["entries"]
                  if item["requirement"]["component_id"] == component["component_id"])
    pin = next(item for item in signed["artifacts"] if item["artifact_id"] == progress["artifact_id"])
    assert progress["component_id"] in snapshot["steps"]
    assert progress["artifact_sha256"] == artifact["sha256"] == pin["sha256"]
    assert progress["total"] == artifact["size"] == pin["size"]
    assert 0 <= progress["completed"] <= progress["total"]
    assert 0 < progress["sequence"] <= snapshot["revision"]
    if progress["state"] == "verified":
        assert progress["completed"] == progress["total"]
    elif progress["state"] == "transferring":
        assert progress["completed"] < progress["total"]
        assert progress["percent"] == progress["completed"] * 100 // progress["total"]


def assert_private_values_absent(value, bundle):
    encoded = json.dumps(value)
    assert "never-public" not in encoded and str(bundle["tmp_path"]) not in encoded
    for url in bundle["transport"].files:
        assert url not in encoded


def collect_pages(read_page, plan, bundle):
    cursor, events, pages = 0, [], 0
    while True:
        page = read_page(cursor)
        assert_schema(page, SCHEMAS["PrerequisiteEventsResult"])
        assert_private_values_absent(page, bundle)
        snapshot = page["snapshot"]
        if "download_progress" in snapshot:
            assert_binding(snapshot["download_progress"], plan, snapshot, bundle)
        if not page["events"]:
            assert cursor == snapshot["revision"]
            break
        pages += 1
        assert pages < 100
        for event in page["events"]:
            assert event["sequence"] == cursor + 1
            assert event["operation_id"] == snapshot["operation_id"]
            cursor = event["sequence"]
            if event["type"] == "prerequisite_download":
                progress = event["download_progress"]
                assert_binding(progress, plan, snapshot, bundle)
                assert event["sequence"] == progress["sequence"]
                assert event["component_id"] == progress["component_id"]
                assert event["timestamp"] == progress["updated_at"]
            events.append(event)
        # Even a terminal snapshot can be ahead of this three-event page.
        if pages == 1:
            assert cursor < snapshot["revision"]
    assert pages > 1
    progress_events = [item for item in events if item["type"] == "prerequisite_download"]
    assert [item["component_id"] for item in progress_events
            if item["download_progress"]["verified"]] == ["base", "client"]
    assert snapshot["download_progress"] == progress_events[-1]["download_progress"]
    return progress_events
