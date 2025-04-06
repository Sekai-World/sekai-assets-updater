from struct import pack, unpack, Struct
from typing import IO, Callable

def offset_decorate(func: Callable):
    def func_wrapper(*args, **kwargs):
        offset = kwargs.get('offset')
        if offset is not None:
            back = args[0].base_stream.tell()
            args[0].base_stream.seek(offset)
            d = func(*args)
            args[0].base_stream.seek(back)
            return d
        return func(*args, **kwargs)

    return func_wrapper

class BinaryStream:
    def __init__(self, base_stream: IO, endian='little'):
        self.base_stream = base_stream
        self.endian = endian

    def readByte(self):
        return self.base_stream.read(1)

    @offset_decorate
    def readBytes(self, length):
        return self.base_stream.read(length)

    def readChar(self):
        return self.unpack('b')

    def readUChar(self):
        return self.unpack('B')

    def readBool(self):
        return self.unpack('?')

    def readInt16(self):
        if (self.endian == 'big'):
            return self.unpack('>h', 2)
        return self.unpack('h', 2)

    def readUInt16(self):
        if (self.endian == 'big'):
            return self.unpack('>H', 2)
        return self.unpack('H', 2)

    def readInt32(self):
        if (self.endian == 'big'):
            return self.unpack('>i', 4)
        return self.unpack('i', 4)

    def readUInt32(self):
        if (self.endian == 'big'):
            return self.unpack('>I', 4)
        return self.unpack('I', 4)

    def readInt64(self):
        if (self.endian == 'big'):
            return self.unpack('>q', 8)
        return self.unpack('q', 8)

    def readUInt64(self):
        if (self.endian == 'big'):
            return self.unpack('>Q', 8)
        return self.unpack('Q', 8)

    def readFloat(self):
        return self.unpack('f', 4)

    def readDouble(self):
        return self.unpack('d', 8)

    def readString(self):
        length = self.readUInt16()
        return self.unpack(str(length) + 's', length)

    @offset_decorate
    def readStringLength(self, length):
        return self.unpack(str(length) + 's', length)

    @offset_decorate
    def readStringToNull(self):
        byte_str = b''
        while 1:
            b = self.readByte()
            if (b == b'\x00'):
                break
            byte_str += b
        return byte_str

    def AlignStream(self, alignment):
        pos = self.base_stream.tell()
        # print('currPos is: ' + str(pos), pos % alignment)
        if ((pos % alignment) != 0):
            self.base_stream.seek(alignment - (pos % alignment), 1)
            # print('aligned currPos is: ' + str(self.base_stream.tell()))

    def writeBytes(self, value):
        self.base_stream.write(value)

    def writeChar(self, value):
        self.pack('c', value)

    def writeUChar(self, value):
        self.pack('C', value)

    def writeBool(self, value):
        self.pack('?', value)

    def writeInt16(self, value):
        self.pack('h', value)

    def writeUInt16(self, value):
        self.pack('H', value)

    def writeInt32(self, value):
        self.pack('i', value)

    def writeUInt32(self, value):
        self.pack('I', value)

    def writeInt64(self, value):
        self.pack('q', value)

    def writeUInt64(self, value):
        self.pack('Q', value)

    def writeFloat(self, value):
        self.pack('f', value)

    def writeDouble(self, value):
        self.pack('d', value)

    def writeString(self, value):
        length = len(value)
        self.writeUInt16(length)
        self.pack(str(length) + 's', value)

    def pack(self, fmt: str, data):
        return self.writeBytes(pack(fmt, data))

    def unpack(self, fmt: str, length=1):
        return unpack(fmt, self.readBytes(length))[0]

    def unpack_raw(self, fmt):
        length = Struct(fmt).size
        return unpack(fmt, self.readBytes(length))
