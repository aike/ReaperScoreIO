"""要求（外枠と各 method の params）の JSON Schema を出力する。

    python3 -m sio_service.schema > docs/sio_rpc.schema.json
"""

from __future__ import annotations

import json
import sys

from pydantic.json_schema import models_json_schema

from . import PROTOCOL_VERSION
from .models import METHODS, Envelope


def build() -> dict:
    models = [(Envelope, "validation")] + [(model, "validation") for model, _ in METHODS.values()]
    seen, unique = set(), []
    for item in models:
        if item[0] not in seen:
            seen.add(item[0])
            unique.append(item)
    refs, schema = models_json_schema(unique, ref_template="#/$defs/{model}")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"Score IO RPC requests (protocol {PROTOCOL_VERSION})",
        "description": "POST /v2/rpc の要求。envelope は外枠、methods は method ごとの params のスキーマ。"
                       "省略可能な項目に null は指定できない。相互排他などの条件は仕様書を参照。",
        "envelope": refs[(Envelope, "validation")],
        "methods": {name: refs[(model, "validation")] for name, (model, _) in METHODS.items()},
        "$defs": schema["$defs"],
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    json.dump(build(), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
