"""Schema $ref must never fetch (SSRF / file read)."""
from __future__ import annotations
import socket, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytest
from portunus_gateway.omni import _validate
from portunus_gateway.registry import RegistryError, _normalize_tools, validate_schema_override
from portunus_gateway.schema_guard import assert_local_schema_refs, iter_off_document_refs
from portunus_shared import PortunusError

def test_local_fragment_ref_ok():
    schema = {"type":"object","properties":{"q":{"$ref":"#/$defs/q"}},"$defs":{"q":{"type":"string"}}}
    assert_local_schema_refs(schema)
    _validate(schema, {"q":"x"}, what="tool arguments")

def test_property_named_ref_is_not_treated_as_ref():
    assert_local_schema_refs({"type":"object","properties":{"schema":{"type":"object","properties":{"$ref":{"type":"string"}}}}})

def test_remote_http_ref_refused_without_fetch():
    hits=[]
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path); self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        def log_message(self,*_a): return
    srv=HTTPServer(("127.0.0.1",0),H); port=srv.server_address[1]
    threading.Thread(target=srv.serve_forever,daemon=True).start()
    try:
        with pytest.raises(PortunusError) as ei:
            _validate({"type":"object","properties":{"q":{"$ref":f"http://127.0.0.1:{port}/x"}}}, {"q":"x"}, what="tool arguments")
        assert ei.value.code=="invalid_tool_arguments" and hits==[]
    finally: srv.shutdown()

def test_file_ref_refused_no_content_in_message(tmp_path):
    secret=tmp_path/"secret.json"; secret.write_text('{"type":"string","enum":["INTERNAL-TOKEN-1"]}')
    with pytest.raises(PortunusError) as ei:
        _validate({"type":"object","properties":{"q":{"$ref":secret.as_uri()}}}, {"q":"guess"}, what="tool arguments")
    assert "INTERNAL-TOKEN-1" not in ei.value.message

def test_self_ref_becomes_typed_error_not_recursion():
    with pytest.raises(PortunusError) as ei:
        _validate({"$ref":"#"}, {"x":1}, what="tool arguments")
    assert ei.value.code=="invalid_tool_arguments"

def test_normalize_tools_rejects_remote_ref():
    with pytest.raises(Exception) as ei:
        _normalize_tools([{"name":"t","inputSchema":{"type":"object","properties":{"q":{"$ref":"http://169.254.169.254/x"}}}}])
    assert "off-document" in str(ei.value).lower() or "169.254" in str(ei.value)

def test_validate_schema_override_rejects_file_ref():
    with pytest.raises(RegistryError) as ei:
        validate_schema_override({"type":"object","properties":{"q":{"$ref":"file:///etc/passwd"}}})
    assert ei.value.code=="invalid_schema"

def test_iter_allows_draft_schema_uri():
    assert iter_off_document_refs({"$schema":"http://json-schema.org/draft-07/schema#","type":"object"})==[]

def test_metadata_url_never_opens_socket():
    opened=[]; real=socket.socket
    class Spy(socket.socket):
        def connect(self, address): opened.append(address); raise OSError("blocked")
        def connect_ex(self, address): opened.append(address); return 111
    socket.socket=Spy  # type: ignore
    try:
        with pytest.raises(PortunusError):
            _validate({"type":"object","properties":{"q":{"$ref":"http://169.254.169.254/x"}}}, {"q":"x"}, what="tool arguments")
        assert opened==[]
    finally: socket.socket=real  # type: ignore
