"""Convert our small, trusted JSON schemas to generateContent's Schema message.

Keep the wire schema structural; size/timestamp/content constraints are checked
locally. Expanding large array limits on the provider can reject valid schemas.
"""
from __future__ import annotations


def response_schema(schema: dict) -> dict:
    def convert(node):
        if "$ref" in node:
            parts = node["$ref"].split("/")
            if parts[:2] != ["#", "$defs"]:
                raise ValueError("Only local schema definitions are supported")
            target = schema
            for part in parts[1:]:
                target = target[part.replace("~1", "/").replace("~0", "~")]
            return convert(target)
        variants = node.get("anyOf")
        if variants:
            non_null = [value for value in variants if value.get("type") != "null"]
            if len(non_null) == 1 and len(non_null) != len(variants):
                return {**convert(non_null[0]), "nullable": True}
            return {"anyOf": [convert(value) for value in variants]}
        kind = node.get("type", "object")
        nullable = isinstance(kind, list) and "null" in kind
        if isinstance(kind, list):
            kinds = [value for value in kind if value != "null"]
            if len(kinds) != 1:
                raise ValueError("Unsupported schema type union")
            kind = kinds[0]
        result = {"type": kind.upper()}
        if nullable:
            result["nullable"] = True
        for key in ("required", "enum"):
            if key in node:
                result[key] = node[key]
        if "properties" in node:
            result["properties"] = {key: convert(value) for key, value in node["properties"].items()}
        if "items" in node:
            result["items"] = convert(node["items"])
        return result
    return convert(schema)
