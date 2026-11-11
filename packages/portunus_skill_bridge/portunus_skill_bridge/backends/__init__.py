from .base import Backend
from .http_rpc import HttpRpcBackend
from .json_cli import JsonCliBackend

__all__ = ["Backend", "HttpRpcBackend", "JsonCliBackend", "build_backend"]


def build_backend(manifest):  # noqa: ANN001
    if manifest.backend == "http_rpc":
        return HttpRpcBackend(manifest)
    if manifest.backend == "json_cli":
        return JsonCliBackend(manifest)
    raise ValueError(f"unsupported backend {manifest.backend!r}")
