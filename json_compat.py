import json
from typing import Any

OPT_INDENT_2 = 1


def dumps(value: Any, option: int | None = None) -> bytes:
    indent = 2 if option == OPT_INDENT_2 else None
    return json.dumps(value, ensure_ascii=False, indent=indent).encode()


def loads(value: bytes | bytearray | str) -> Any:
    return json.loads(value)
