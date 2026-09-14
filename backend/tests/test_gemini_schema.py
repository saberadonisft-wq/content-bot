import pytest
from pydantic import TypeAdapter

from app.services.gemini_merge import GENERATION_RESPONSE_SCHEMA
from app.services.gemini_review import RawProposal
from app.services.gemini_schema import response_schema


def test_generation_wire_schema_keeps_required_source_fields_and_nullable_values():
    schema = response_schema(GENERATION_RESPONSE_SCHEMA)
    assert schema["type"] == "OBJECT" and schema["required"] == ["segments"]
    cue = schema["properties"]["segments"]["items"]
    assert set(cue["required"]) >= {"start_ms", "end_ms", "text", "source_text", "source_language"}
    assert cue["properties"]["source_text"] == {"type": "STRING", "nullable": True}
    assert "maxItems" not in schema["properties"]["segments"]


def test_review_wire_schema_expands_definitions_without_foreign_fields():
    original = TypeAdapter(list[RawProposal]).json_schema()
    converted = response_schema(original)
    assert converted["type"] == "ARRAY"
    proposal = converted["items"]
    assert proposal["properties"]["operation"]["enum"] == ["edit", "add", "delete", "merge", "split", "retime"]
    after = proposal["properties"]["after"]["items"]
    assert after["properties"]["source_language"]["nullable"]
    def visit(value):
        if isinstance(value, dict):
            assert not set(value) & {"$ref", "$defs", "additionalProperties", "default", "maxItems"}
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(converted)
    with pytest.raises(ValueError):
        response_schema({"$ref": "https://example.com/schema"})
