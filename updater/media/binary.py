from struct import Struct, pack, unpack
from typing import IO, Callable


def offset_decorate(func: Callable):
    def func_wrapper(*args, **kwargs):
        offset = kwargs.get("offset")
        if offset is not None:
            back = args[0].base_stream.tell()
            args[0].base_stream.seek(offset)
            try:
                d = func(*args)
            finally:
                args[0].base_stream.seek(back)
            return d
        return func(*args, **kwargs)

    return func_wrapper


class BinaryStream:
    def __init__(self, base_stream: IO, endian="little"):
        self.base_stream = base_stream
        self.endian = endian

    def read_byte(self):
        return self.base_stream.read(1)

    @offset_decorate
    def read_bytes(self, length):
        return self.base_stream.read(length)

    def read_char(self):
        return self.unpack("b")

    def read_uchar(self):
        return self.unpack("B")

    def read_bool(self):
        return self.unpack("?")

    def read_int16(self):
        if self.endian == "big":
            return self.unpack(">h", 2)
        return self.unpack("h", 2)

    def read_uint16(self):
        if self.endian == "big":
            return self.unpack(">H", 2)
        return self.unpack("H", 2)

    def read_int32(self):
        if self.endian == "big":
            return self.unpack(">i", 4)
        return self.unpack("i", 4)

    def read_uint32(self):
        if self.endian == "big":
            return self.unpack(">I", 4)
        return self.unpack("I", 4)

    def read_int64(self):
        if self.endian == "big":
            return self.unpack(">q", 8)
        return self.unpack("q", 8)

    def read_uint64(self):
        if self.endian == "big":
            return self.unpack(">Q", 8)
        return self.unpack("Q", 8)

    def read_float(self):
        return self.unpack("f", 4)

    def read_double(self):
        return self.unpack("d", 8)

    def read_string(self):
        length = self.read_uint16()
        return self.unpack(str(length) + "s", length)

    @offset_decorate
    def read_string_length(self, length):
        return self.unpack(str(length) + "s", length)

    @offset_decorate
    def read_string_to_null(self):
        byte_str = b""
        while 1:
            b = self.read_byte()
            if b == b"":
                raise EOFError("null-terminated string is missing its terminator")
            if b == b"\x00":
                break
            byte_str += b
        return byte_str

    def align_stream(self, alignment):
        pos = self.base_stream.tell()
        # print('currPos is: ' + str(pos), pos % alignment)
        if (pos % alignment) != 0:
            self.base_stream.seek(alignment - (pos % alignment), 1)
            # print('aligned currPos is: ' + str(self.base_stream.tell()))

    def write_bytes(self, value):
        self.base_stream.write(value)

    def write_char(self, value):
        self.pack("c", value)

    def write_uchar(self, value):
        self.pack("C", value)

    def write_bool(self, value):
        self.pack("?", value)

    def write_int16(self, value):
        self.pack("h", value)

    def write_uint16(self, value):
        self.pack("H", value)

    def write_int32(self, value):
        self.pack("i", value)

    def write_uint32(self, value):
        self.pack("I", value)

    def write_int64(self, value):
        self.pack("q", value)

    def write_uint64(self, value):
        self.pack("Q", value)

    def write_float(self, value):
        self.pack("f", value)

    def write_double(self, value):
        self.pack("d", value)

    def writeString(self, value):
        length = len(value)
        self.write_uint16(length)
        self.pack(str(length) + "s", value)

    def pack(self, fmt: str, data):
        return self.write_bytes(pack(fmt, data))

    def unpack(self, fmt: str, length=1):
        return unpack(fmt, self.read_bytes(length))[0]

    def unpack_raw(self, fmt):
        length = Struct(fmt).size
        return unpack(fmt, self.read_bytes(length))
