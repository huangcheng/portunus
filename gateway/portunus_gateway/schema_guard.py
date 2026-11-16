"""JSON Schema guards: no remote/file $ref resolution during validation or ingest.

Tool schemas are upstream data. Argument validation must never perform I/O
(SSRF / local file read / event-loop stall). See security audit 2026-11-16 Finding 1.
"""

from __future__ import annotations

from typing import Any

from referencing import Registry

# Resolves nothing — blocks jsonschema's default urllib retriever.
EMPTY_REGISTRY: Registry = Registry()

# Metaschema URIs ``check_schema`` / authors may declare; never fetched by us.
_ALLOWED_SCHEMA_URIS = frozenset(
    {
        "http://json-schema.org/schema#",
        "http://json-schema.org/draft-04/schema",
        "http://json-schema.org/draft-04/schema#",
        "https://json-schema.org/draft-04/schema",
        "https://json-schema.org/draft-04/schema#",
        "http://json-schema.org/draft-06/schema",
        "http://json-schema.org/draft-06/schema#",
        "https://json-schema.org/draft-06/schema",
        "https://json-schema.org/draft-06/schema#",
        "http://json-schema.org/draft-07/schema",
        "http://json-schema.org/draft-07/schema#",
        "https://json-schema.org/draft-07/schema",
        "https://json-schema.org/draft-07/schema#",
        "https://json-schema.org/draft/2019-09/schema",
        "https://json-schema.org/draft/2019-09/schema#",
        "https://json-schema.org/draft/2020-12/schema",
        "https://json-schema.org/draft/2020-12/schema#",
    }
)

_REF_KEYS = frozenset({"$ref", "$dynamicRef", "$recursiveRef"})


def _is_local_ref(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("#")


def iter_off_document_refs(node: Any, *, path: str = "") -> list[tuple[str, str]]:
    """Return ``(json_pointer, ref_value)`` for every non-fragment ref in ``node``."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, val in node.items():
            here = f"{path}/{key}" if path else f"/{key}"
            if key in _REF_KEYS:
                # Only string values are JSON Schema refs. A property *named*
                # ``$ref`` whose value is a schema object is input data, not a ref.
                if isinstance(val, str):
                    if not _is_local_ref(val):
                        found.append((here, val))
                else:
                    found.extend(iter_off_document_refs(val, path=here))
            elif key in {"const", "enum", "default", "examples", "example"}:
                # Instance data, not schemas — a nested "$ref" key is not a JSON Schema ref.
                continue
            else:
                found.extend(iter_off_document_refs(val, path=here))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            found.extend(iter_off_document_refs(item, path=f"{path}/{i}"))
    return found


def assert_local_schema_refs(schema: Any, *, what: str = "schema") -> None:
    """Raise ``ValueError`` if ``schema`` contains a non-local ``$ref`` / bad ``$schema``."""
    if not isinstance(schema, dict):
        return
    bad = iter_off_document_refs(schema)
    if not bad:
        return
    ptr, ref = bad[0]
    raise ValueError(f"{what}: off-document reference at {ptr}: {ref[:120]}")
