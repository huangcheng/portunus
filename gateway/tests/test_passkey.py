"""WebAuthn counter clone detection and CBOR bounds."""

from __future__ import annotations

import hashlib
import json

import pytest
from portunus_gateway.passkey import (
    PasskeyError,
    _cbor_read,
    b64url_encode,
    set_allowed_origins,
    verify_assertion,
)


def test_cbor_rejects_truncated_and_deep_input():
    with pytest.raises(PasskeyError) as empty:
        _cbor_read(b"", 0)
    assert empty.value.code == "passkey_format"
    with pytest.raises(PasskeyError):
        _cbor_read(bytes([0x18]), 0)  # uint8 missing its value byte
    with pytest.raises(PasskeyError):
        _cbor_read(bytes([0x1A, 0x00, 0x01]), 0)  # uint32 truncated
    value, pos = _cbor_read(bytes([0xA1, 0x01, 0x02]), 0)
    assert value == {1: 2} and pos == 3
    with pytest.raises(PasskeyError) as deep:
        _cbor_read(b"\x81" * 12, 0)
    assert "nesting" in deep.value.message


def _assertion_code(counter: int, *, stored: int) -> str | None:
    rp = "portal.example"
    origin = "https://portal.example"
    set_allowed_origins({origin})
    challenge = b"\x11" * 32
    client = json.dumps(
        {"type": "webauthn.get", "challenge": b64url_encode(challenge), "origin": origin}
    ).encode()
    authdata = hashlib.sha256(rp.encode()).digest() + bytes([0x01]) + counter.to_bytes(4, "big")
    try:
        verify_assertion(
            credential_id_b64="cred",
            client_data_b64=b64url_encode(client),
            authdata_b64=b64url_encode(authdata),
            signature_b64=b64url_encode(b"not-a-signature"),
            expected_challenge=challenge,
            expected_rp_id=rp,
            stored_public_key_hex="00",
            stored_sign_count=stored,
            stored_credential_id="cred",
            allowed_origins={origin},
        )
    except PasskeyError as exc:
        return exc.code
    except Exception:
        return None
    return None


def test_counter_must_advance_once_known():
    try:
        assert _assertion_code(0, stored=5) == "passkey_counter"
        assert _assertion_code(5, stored=5) == "passkey_counter"
        assert _assertion_code(4, stored=5) == "passkey_counter"
        # A stored count of 0 still allows a non-incrementing authenticator.
        assert _assertion_code(0, stored=0) != "passkey_counter"
        # An advancing counter is not a clone; signature verification happens after.
        assert _assertion_code(6, stored=5) != "passkey_counter"
    finally:
        set_allowed_origins(set())
