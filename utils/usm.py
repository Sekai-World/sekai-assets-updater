#!/usr/bin/env python3
# usm.py: Extracting USM video and audio file

import struct
import sys
from io import BytesIO
from os import path

from utils.binary import BinaryStream

COLUMN_STORAGE_MASK = 0xF0
COLUMN_STORAGE_PERROW = 0x50
COLUMN_STORAGE_CONSTANT = 0x30
COLUMN_STORAGE_CONSTANT2 = 0x70
COLUMN_STORAGE_ZERO = 0x10

COLUMN_TYPE_MASK = 0x0F
COLUMN_TYPE_DATA = 0x0B
COLUMN_TYPE_STRING = 0x0A
COLUMN_TYPE_FLOAT = 0x08
COLUMN_TYPE_8BYTE = 0x06
COLUMN_TYPE_4BYTE2 = 0x05
COLUMN_TYPE_4BYTE = 0x04
COLUMN_TYPE_2BYTE2 = 0x03
COLUMN_TYPE_2BYTE = 0x02
COLUMN_TYPE_1BYTE2 = 0x01
COLUMN_TYPE_1BYTE = 0x00

# string and data fields require more information
def promise_data(r):
    offset = r.readUInt32()
    size = r.readUInt32()
    return lambda data_offset: r.readBytes(size, offset=data_offset + offset - 24)


def promise_string(r):
    offset = r.readUInt32()
    return lambda string_table_offset: r.readStringToNull(
        offset=string_table_offset + offset - 24
    )


column_data_dtable = {
    COLUMN_TYPE_DATA: promise_data,
    COLUMN_TYPE_STRING: promise_string,
    COLUMN_TYPE_FLOAT: lambda r: r.readFloat(),
    COLUMN_TYPE_8BYTE: lambda r: r.readUInt64(),
    COLUMN_TYPE_4BYTE2: lambda r: r.readInt(),
    COLUMN_TYPE_4BYTE: lambda r: r.readUInt32(),
    COLUMN_TYPE_2BYTE2: lambda r: r.readInt16(),
    COLUMN_TYPE_2BYTE: lambda r: r.readUInt16(),
    COLUMN_TYPE_1BYTE2: lambda r: r.readChar(),
    COLUMN_TYPE_1BYTE: lambda r: r.readUChar(),
}

column_data_stable = {
    COLUMN_TYPE_DATA: "8s",
    COLUMN_TYPE_STRING: "4s",
    COLUMN_TYPE_FLOAT: "f",
    COLUMN_TYPE_8BYTE: "Q",
    COLUMN_TYPE_4BYTE2: "i",
    COLUMN_TYPE_4BYTE: "I",
    COLUMN_TYPE_2BYTE2: "h",
    COLUMN_TYPE_2BYTE: "H",
    COLUMN_TYPE_1BYTE2: "b",
    COLUMN_TYPE_1BYTE: "B",
}

utf_header_t = ">IHHIIIHHI"


def get_utf_table(usm_file):
    assert usm_file.readStringLength(4) == b"@UTF"

    header = usm_file.unpack_raw(utf_header_t)
    table_size = header[0]
    row_offset = header[2]
    string_table_offset = header[3]
    data_offset = header[4]
    table_name_offset = header[5]
    number_of_fields = header[6]
    number_of_rows = header[8]

    utf_table = BinaryStream(BytesIO(usm_file.readBytes(table_size - 24)), "big")
    name = utf_table.readStringToNull(
        offset=string_table_offset + table_name_offset - 24
    )

    dynamic_keys = []
    format = ">"
    constants = {}
    for i in range(number_of_fields):
        field_type = utf_table.readUChar()
        name_offset = utf_table.readUInt32()

        occurrence = field_type & COLUMN_STORAGE_MASK
        type_key = field_type & COLUMN_TYPE_MASK

        if occurrence in (COLUMN_STORAGE_CONSTANT, COLUMN_STORAGE_CONSTANT2):
            field_name = utf_table.readStringToNull(
                offset=string_table_offset + name_offset - 24
            )
            field_val = column_data_dtable[type_key](utf_table)
            constants[name] = field_val
        else:
            dynamic_keys.append(
                utf_table.readStringToNull(
                    offset=string_table_offset + name_offset - 24
                )
            )
            format += column_data_stable[type_key]

    utf_table.base_stream.seek(row_offset - 24)
    rows = []
    for n in range(number_of_rows):
        values = utf_table.unpack_raw(format)
        tmp = []
        for val in values:
            if isinstance(val, bytes):
                if len(val) == 8:
                    offset, size = struct.unpack(">II", val)
                    tmp.append(
                        utf_table.readBytes(size, offset=data_offset + name_offset - 24)
                    )
                else:
                    offset = struct.unpack(">I", val)[0]
                    tmp.append(
                        utf_table.readStringToNull(
                            offset=string_table_offset + offset - 24
                        )
                    )
            else:
                tmp.append(val)
        ret = {k: v for k, v in zip(dynamic_keys, tuple(tmp))}
        ret.update(constants)
        rows.append(ret)

    return rows


def get_mask(key):
    key1 = key & 0xFFFFFFFF
    key2 = (key >> 64) & 0xFFFFFFFF

    t = bytearray(0x20)
    t[0x00] = key1 & 0xFF
    t[0x01] = (key1 >> 8) & 0xFF
    t[0x02] = (key1 >> 16) & 0xFF
    t[0x03] = (((key1 >> 24) & 0xFF) - 0x34) & 0xFF
    t[0x04] = ((key2 & 0xF) + 0xF9) & 0xFF
    t[0x05] = ((key2 >> 8) & 0xFF) ^ 0x13
    t[0x06] = (((key2 >> 16) & 0xFF) + 0x61) & 0xFF
    t[0x07] = t[0x00] ^ 0xFF
    t[0x08] = (t[0x02] + t[0x01]) & 0xFF
    t[0x09] = (t[0x01] - t[0x07]) & 0xFF
    t[0x0A] = t[0x02] ^ 0xFF
    t[0x0B] = t[0x01] ^ 0xFF
    t[0x0C] = (t[0x0B] + t[0x09]) & 0xFF
    t[0x0D] = (t[0x08] - t[0x03]) & 0xFF
    t[0x0E] = t[0x0D] ^ 0xFF
    t[0x0F] = (t[0x0A] - t[0x0B]) & 0XFF
    t[0x10] = (t[0x08] - t[0x0F]) & 0xFF
    t[0x11] = t[0x10] ^ t[0x07]
    t[0x12] = t[0x0F] ^ 0xFF
    t[0x13] = t[0x03] ^ 0x10
    t[0x14] = (t[0x04] - 0x32) & 0xFF
    t[0x15] = (t[0x05] + 0xED) & 0xFF
    t[0x16] = t[0x06] ^ 0xF3
    t[0x17] = (t[0x13] - t[0x0F]) & 0xFF
    t[0x18] = (t[0x15] + t[0x07]) & 0xFF
    t[0x19] = (0x21 - t[0x13]) & 0xFF
    t[0x1A] = t[0x14] ^ t[0x17]
    t[0x1B] = (t[0x16] + t[0x16]) & 0xFF
    t[0x1C] = (t[0x17] + 0x44) & 0xFF
    t[0x1D] = (t[0x03] + t[0x04]) & 0xFF
    t[0x1E] = (t[0x05] - t[0x16]) & 0xFF
    t[0x1F] = (t[0x1D] ^ t[0x13]) & 0xFF

    t2 = b"URUC"
    vmask1 = bytearray(0x20)
    vmask2 = bytearray(0x20)
    amask = bytearray(0x20)
    for i, ti in enumerate(t):
        vmask1[i] = ti
        vmask2[i] = ti ^ 0xFF
        # print(i, vmask2[i])
        amask[i] = t2[(i >> 1) & 3] if i & 1 else ti ^ 0xFF

    return (vmask1, vmask2), amask


def mask_video(content, vmask):
    _content = bytearray(content)
    size = len(_content) - 0x40
    base = 0x40

    if size >= 0x200:
        mask = bytearray(vmask[1])
        for i in range(0x100, size):
            _content[base + i] ^= mask[i & 0x1F]
            mask[i & 0x1F] = _content[base + i] ^ vmask[1][i & 0x1F]

        mask = bytearray(vmask[0])
        for i in range(0x100):
            mask[i & 0x1F] ^= _content[0x100 + base + i]
            _content[base + i] ^= mask[i & 0x1F]

    return _content


def mask_audio(content, amask):
    _content = bytearray(content)
    size = len(_content) - 0x140
    base = 0x140
    for i in range(size):
        _content[base + i] ^= amask[i & 0x1F]

    return _content


def extract_usm(usm, target_dir, *args):
    usm_file = BinaryStream(usm, "big")
    
    offset = 0
    vmask = None
    amask = None
    if len(args):
        vmask, amask = get_mask(int(args[0]))

    assert usm_file.readStringLength(4) == b"CRID"
    block_size = usm_file.readUInt32()
    usm_file.base_stream.seek(0x20)
    entry_table = get_utf_table(usm_file)
    filename = entry_table[len(entry_table) - 1][b"filename"]
    offset += 8 + block_size

    usm_file.base_stream.seek(offset)
    assert usm_file.readStringLength(4) == b"@SFV"
    block_size = usm_file.readUInt32()
    usm_file.base_stream.seek(offset + 0x20)
    video_meta_table = get_utf_table(usm_file)
    offset += 8 + block_size

    usm_file.base_stream.seek(offset)
    next_sig = usm_file.readStringLength(4)
    hasAudio = False
    if next_sig == b"@SFA":
        block_size = usm_file.readUInt32()
        usm_file.base_stream.seek(offset + 0x20)
        audio_meta_table = get_utf_table(usm_file)
        offset += 8 + block_size
        hasAudio = True
        usm_file.base_stream.seek(offset)
        next_sig = usm_file.readStringLength(4)
    assert next_sig == b"@SFV"
    block_size = usm_file.readUInt32()
    usm_file.base_stream.seek(offset + 0x20)
    assert usm_file.readStringLength(11) == b"#HEADER END"
    offset += 8 + block_size

    if hasAudio:
        usm_file.base_stream.seek(offset)
        assert usm_file.readStringLength(4) == b"@SFA"
        block_size = usm_file.readUInt32()
        usm_file.base_stream.seek(offset + 0x20)
        assert usm_file.readStringLength(11) == b"#HEADER END"
        offset += 8 + block_size

    usm_file.base_stream.seek(offset)
    assert usm_file.readStringLength(4) == b"@SFV"
    block_size = usm_file.readUInt32()
    usm_file.base_stream.seek(offset + 0x20)
    video_data_table = get_utf_table(usm_file)
    offset += 8 + block_size

    usm_file.base_stream.seek(offset)
    assert usm_file.readStringLength(4) == b"@SFV"
    usm_file.base_stream.seek(28, 1)
    assert usm_file.readStringLength(13) == b"#METADATA END"
    usm_file.AlignStream(4)

    usm_file.base_stream.seek(16, 1)
    # dive into video blocks
    decoded_filename = ''
    try:
        decoded_filename = filename.decode("shift-jis")
    except UnicodeDecodeError:
        decoded_filename = filename.decode("cp932")
    output_files = [
        open(
            path.join(target_dir, path.splitext(decoded_filename)[0] + ".m2v"),
            "wb",
        )
    ]
    if hasAudio:
        output_files.append(
            open(
                path.join(
                    target_dir, path.splitext(decoded_filename)[0] + ".adx"
                ),
                "wb",
            )
        )
    while 1:
        next_sig = usm_file.readStringLength(4)
        block_size = usm_file.readUInt32()
        next_offset = usm_file.base_stream.tell() + block_size
        chunk_header_size = usm_file.readUInt16()
        chunk_footer_size = usm_file.readUInt16()
        # print(chunk_footer_size, chunk_header_size)
        usm_file.readBytes(3)  # skip 3 bytes
        data_type = usm_file.readChar() & 0b11
        usm_file.base_stream.seek(16, 1)
        if usm_file.readStringLength(13) == b"#CONTENTS END":
            break
        usm_file.base_stream.seek(-13, 1)
        read_data_len = block_size - chunk_header_size - chunk_footer_size
        if next_sig == b"@SFV":
            content = usm_file.readBytes(read_data_len)
            if data_type == 0 and vmask is not None:
                # encrypted
                content = mask_video(content, vmask)
            output_files[0].write(content)
        elif next_sig == b"@SFA":
            content = usm_file.readBytes(read_data_len)
            if data_type == 0 and vmask is not None:
                # encrypted
                content = mask_audio(content, amask)
            output_files[1].write(content)
        usm_file.base_stream.seek(next_offset)

    output_files[0].close()
    if hasAudio:
        output_files[1].close()

    return list(map(lambda x: x.name, output_files))


def main(invocation, usm_file, target_dir, *args):
    # args[0] = key (decimal)
    with open(usm_file, "rb") as usm:
        extract_usm(usm, target_dir, *args)


if __name__ == "__main__":
    main(*sys.argv)
