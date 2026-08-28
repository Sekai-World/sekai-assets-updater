"""moc3 parameter-id recovery via CRC32 matching."""

import logging
from io import BytesIO
from typing import Dict
from zlib import crc32

from updater.media.binary import BinaryStream

logger = logging.getLogger("live2d")


def extract_params_ids_from_moc3(moc3: bytes) -> Dict[str, str]:
    """Extract parameter IDs from moc3 file"""
    bs = BinaryStream(BytesIO(moc3))
    bs.base_stream.seek(0x4C)
    part_base_addr = bs.readUInt32()
    part_end_addr = bs.readUInt32()

    cursor = part_base_addr
    param_id_map = {}

    while part_end_addr - cursor > 64:
        bs.base_stream.seek(cursor)
        param_id = bs.readStringToNull()
        crc = str(crc32(param_id))
        param_id_map[crc] = param_id.decode()
        crc = str(crc32(b"Parts/" + param_id))
        param_id_map[crc] = param_id.decode()

        cursor += 64

    bs.base_stream.seek(0x108)
    param_base_addr = bs.readUInt32()
    param_end_addr = bs.readUInt32()

    cursor = param_base_addr

    while param_end_addr - cursor > 64:
        bs.base_stream.seek(cursor)
        param_id = bs.readStringToNull()
        crc = str(crc32(param_id))
        param_id_map[crc] = param_id.decode()
        crc = str(crc32(b"Parameters/" + param_id))
        param_id_map[crc] = param_id.decode()

        cursor += 64

    return param_id_map
