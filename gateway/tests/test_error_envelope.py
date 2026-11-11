"""Every declared ERROR_CODES entry must produce a well-formed MCP isError envelope.

Review #24: new codes were raised from gateway code while the installed
``portunus_shared`` package lagged behind. Construction then raised
``ValueError('unknown error code: …')``, which escaped as JSON-RPC ``code: 0``
instead of ``result.isError`` + ``structuredContent.error``.

This test locks the mapping: if a code is in ``ERROR_CODES``, ``PortunusError``
constructs and ``error_result`` returns a tool-result failure with that code.
"""

from __future__ import annotations

import json

import pytest

from portunus_gateway.omni import error_result
from portunus_shared import ERROR_CODES, PortunusError


@pytest.mark.parametrize("code", sorted(ERROR_CODES))
def test_portunus_error_constructs_for_every_declared_code(code: str) -> None:
    err = PortunusError(code, f"detail for {code}")
    assert err.code == code
    assert f"detail for {code}" in err.message


@pytest.mark.parametrize("code", sorted(ERROR_CODES))
def test_error_result_is_well_formed_iserror_for_every_declared_code(code: str) -> None:
    result = error_result(code, f"detail for {code}", data={"path": "arguments.limit"})
    assert result.is_error is True
    body = result.structured_content
    assert isinstance(body, dict)
    err = body["error"]
    assert err["code"] == code
    assert err["message"] == f"detail for {code}"
    assert err["data"] == {"path": "arguments.limit"}
    # Text content must be the same JSON (clients that only read content[]).
    assert json.loads(result.content[0].text) == body


def test_error_result_remaps_unknown_code_instead_of_crashing() -> None:
    result = error_result("not_a_real_code", "should remap")
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "upstream_crash"


def test_unknown_code_cannot_construct_portunus_error() -> None:
    with pytest.raises(ValueError, match="unknown error code"):
        PortunusError("not_a_real_code", "x")
