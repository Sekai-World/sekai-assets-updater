from io import BytesIO

import pytest

from utils.binary import BinaryStream


def test_read_string_to_null_returns_bytes_before_terminator() -> None:
    stream = BinaryStream(BytesIO(b"hello\x00trailing"))

    assert stream.readStringToNull() == b"hello"


@pytest.mark.parametrize("data", [b"", b"unterminated"])
def test_read_string_to_null_raises_eof_without_terminator(data: bytes) -> None:
    with pytest.raises(EOFError):
        BinaryStream(BytesIO(data)).readStringToNull()


def test_read_string_to_null_with_offset_restores_position() -> None:
    base_stream = BytesIO(b"prefixvalue\x00suffix")
    base_stream.seek(2)
    stream = BinaryStream(base_stream)

    assert stream.readStringToNull(offset=6) == b"value"
    assert base_stream.tell() == 2


def test_read_string_to_null_with_offset_eof_restores_position() -> None:
    base_stream = BytesIO(b"prefixunterminated")
    base_stream.seek(2)
    stream = BinaryStream(base_stream)

    with pytest.raises(EOFError):
        stream.readStringToNull(offset=6)

    assert base_stream.tell() == 2
