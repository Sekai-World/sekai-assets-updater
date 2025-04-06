#!/usr/bin/env python3
# acb.py: For all your ACB extracting needs

# Copyright (c) 2016, The Holy Constituency of the Summer Triangle.
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# This program is based on code from VGMToolbox.
# Copyright (c) 2009
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.

import functools
import io
import itertools
import math
import os
import struct
import sys
from collections import namedtuple as T
from typing import BinaryIO

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

WAVEFORM_ENCODE_TYPE_ADX = 0
WAVEFORM_ENCODE_TYPE_HCA = 2
WAVEFORM_ENCODE_TYPE_VAG = 7
WAVEFORM_ENCODE_TYPE_ATRAC3 = 8
WAVEFORM_ENCODE_TYPE_BCWAV = 9
WAVEFORM_ENCODE_TYPE_NINTENDO_DSP = 13


# string and data fields require more information
def promise_data(r):
    offset = r.uint32_t()
    size = r.uint32_t()
    return lambda h: r.bytes(size, at=h.data_offset + 8 + offset)


def promise_string(r):
    offset = r.uint32_t()
    return lambda h: r.string0(at=h.string_table_offset + 8 + offset)


column_data_dtable = {
    COLUMN_TYPE_DATA: promise_data,
    COLUMN_TYPE_STRING: promise_string,
    COLUMN_TYPE_FLOAT: lambda r: r.float32_t(),
    COLUMN_TYPE_8BYTE: lambda r: r.uint64_t(),
    COLUMN_TYPE_4BYTE2: lambda r: r.int32_t(),
    COLUMN_TYPE_4BYTE: lambda r: r.uint32_t(),
    COLUMN_TYPE_2BYTE2: lambda r: r.int16_t(),
    COLUMN_TYPE_2BYTE: lambda r: r.uint16_t(),
    COLUMN_TYPE_1BYTE2: lambda r: r.int8_t(),
    COLUMN_TYPE_1BYTE: lambda r: r.uint8_t()
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
    COLUMN_TYPE_1BYTE: "B"
}

wave_type_ftable = {
    WAVEFORM_ENCODE_TYPE_ADX: ".adx",
    WAVEFORM_ENCODE_TYPE_HCA: ".hca",
    WAVEFORM_ENCODE_TYPE_VAG: ".at3",
    WAVEFORM_ENCODE_TYPE_ATRAC3: ".vag",
    WAVEFORM_ENCODE_TYPE_BCWAV: ".bcwav",
    WAVEFORM_ENCODE_TYPE_NINTENDO_DSP: ".dsp"
}


class R(object):
    """ file reader based on types """

    def __init__(self, file):
        self.f = file

    @staticmethod
    def readfunc(fmt: str | bytes):
        a = struct.Struct(fmt)
        b = a.size

        def f(f: BinaryIO, at: int | None = None) -> int:
            if at is not None:
                back = f.tell()
                f.seek(at)
                d = a.unpack(f.read(b))[0]
                f.seek(back)
                return d
            else:
                return a.unpack(f.read(b))[0]

        return f

    @staticmethod
    def latebinder(f):
        return lambda s: f(s.f)

    int8_t = latebinder(readfunc(">b"))
    uint8_t = latebinder(readfunc(">B"))
    int16_t = latebinder(readfunc(">h"))
    uint16_t = latebinder(readfunc(">H"))
    int32_t = latebinder(readfunc(">i"))
    uint32_t = latebinder(readfunc(">I"))
    int64_t = latebinder(readfunc(">q"))
    uint64_t = latebinder(readfunc(">Q"))
    float32_t = latebinder(readfunc(">f"))

    le_int8_t = latebinder(readfunc("<b"))
    le_uint8_t = latebinder(readfunc("<B"))
    le_int16_t = latebinder(readfunc("<h"))
    le_uint16_t = latebinder(readfunc("<H"))
    le_int32_t = latebinder(readfunc("<i"))
    le_uint32_t = latebinder(readfunc("<I"))
    le_int64_t = latebinder(readfunc("<q"))
    le_uint64_t = latebinder(readfunc("<Q"))
    le_float32_t = latebinder(readfunc("<f"))

    def seek(self, at, where=os.SEEK_SET):
        self.f.seek(at, where)

    def struct(self, struct, at=None):
        if at is not None:
            back = self.f.tell()
            self.f.seek(at)
            d = self.struct(struct)
            self.f.seek(back)
            return d

        return struct.unpack(self.f.read(struct.size))

    def bytes(self, size, at=None):
        if at is not None:
            back = self.f.tell()
            self.f.seek(at)
            d = self.bytes(size)
            self.f.seek(back)
            return d

        return self.f.read(size)

    def string0(self, at=None):
        if at is not None:
            back = self.f.tell()
            self.f.seek(at)
            d = self.string0()
            self.f.seek(back)
            return d

        bk = self.f.tell()
        tl = 0
        sr = []
        while 1:
            b = self.f.read(16)
            tl += len(b)

            if len(b) == 0:
                raise Exception("EOF")

            for c in b:
                if c != 0:
                    sr.append(c)
                else:
                    break
            else:
                continue
            break
        string = bytes(sr)
        self.f.seek(bk + len(string) + 1)
        return string.decode("utf8")


class Struct(struct.Struct):
    """ struct with an output filter (usually a namedtuple) """

    def __init__(self, fmt, out_type):
        super().__init__(fmt)
        self.out_type = out_type

    def unpack(self, buf):
        return self.out_type(*super().unpack(buf))


utf_header_t = Struct(
    ">IHHIIIHHI",
    T("utf_header_t",
      ("table_size", "u1", "row_offset", "string_table_offset", "data_offset",
       "table_name_offset", "number_of_fields", "row_size", "number_of_rows")))


class UTFTable(object):

    def __init__(self, file: io.BytesIO | io.BufferedReader):
        if isinstance(file, io.BytesIO):
            assert file.getbuffer().nbytes != 0
        buf = R(file)
        magic = buf.uint32_t()
        if magic != 0x40555446:
            raise ValueError("bad magic")

        self.header = buf.struct(utf_header_t)
        self.name = buf.string0(at=self.header.string_table_offset + 8 +
                                self.header.table_name_offset)

        buf.seek(0x20)
        self.read_schema(buf)

        buf.seek(self.header.row_offset + 8)
        self.rows = list(self.iter_rows(buf))

    def read_schema(self, buf):
        buf.seek(0x20)

        dynamic_keys = []
        format = ">"
        constants = {}

        for _ in range(self.header.number_of_fields):
            field_type = buf.uint8_t()
            name_offset = buf.uint32_t()

            occurrence = field_type & COLUMN_STORAGE_MASK
            type_key = field_type & COLUMN_TYPE_MASK

            if occurrence in (COLUMN_STORAGE_CONSTANT,
                              COLUMN_STORAGE_CONSTANT2):
                name = buf.string0(at=self.header.string_table_offset + 8 +
                                   name_offset)
                val = column_data_dtable[type_key](buf)
                constants[name] = val
            else:
                dynamic_keys.append(
                    buf.string0(at=self.header.string_table_offset + 8 +
                                name_offset))
                format += column_data_stable[type_key]

        for k in constants.keys():
            if callable(constants[k]):
                constants[k] = constants[k](self.header)

        self.dynamic_keys = dynamic_keys
        self.struct_format = format
        self.constants = constants

    def resolve(self, buf, *args):
        ret = []
        for val in args:
            if isinstance(val, bytes):
                if len(val) == 8:
                    offset, size = struct.unpack(">II", val)
                    ret.append(
                        buf.bytes(size,
                                  at=self.header.data_offset + 8 + offset))
                else:
                    offset = struct.unpack(">I", val)[0]
                    ret.append(
                        buf.string0(at=self.header.string_table_offset + 8 +
                                    offset))
            else:
                ret.append(val)
        return tuple(ret)

    def iter_rows(self, buf):
        sfmt = Struct(self.struct_format, functools.partial(self.resolve, buf))
        for n in range(self.header.number_of_rows):
            values = buf.struct(sfmt)
            ret = {k: v for k, v in zip(self.dynamic_keys, values)}
            ret.update(self.constants)
            yield ret


track_t = T(
    "track_t",
    ("cue_id", "name", "wav_id", "enc_type", "is_stream", "stream_awb_id"))


class TrackList(object):

    def __init__(self, utf):
        cue_handle = io.BytesIO(utf.rows[0]["CueTable"])
        nam_handle = io.BytesIO(utf.rows[0]["CueNameTable"])
        wav_handle = io.BytesIO(utf.rows[0]["WaveformTable"])
        syn_handle = io.BytesIO(utf.rows[0]["SynthTable"])
        tra_handle = io.BytesIO(utf.rows[0]["TrackTable"])
        tev_handle = io.BytesIO(utf.rows[0].get("TrackEventTable", None)
                                or utf.rows[0].get("CommandTable"))
        seq_handle = io.BytesIO(utf.rows[0]["SequenceTable"])
        # seq_comm_handle = io.BytesIO(utf.rows[0].get("SeqCommandTable", None))

        cues = UTFTable(cue_handle)
        nams = UTFTable(nam_handle)
        wavs = UTFTable(wav_handle)
        syns = UTFTable(syn_handle)
        tras = UTFTable(tra_handle)
        tevs = UTFTable(tev_handle)
        try:
            seqs = UTFTable(seq_handle)
        except AssertionError:
            seqs = None
        # seq_comms = UTFTable(seq_comm_handle)

        self.tracks = []

        name_map = {}
        for row in nams.rows:
            name_map[row["CueIndex"]] = row["CueName"]

        for row in cues.rows:
            if row["ReferenceType"] not in {3, 8}:
                raise RuntimeError("ReferenceType {0} not implemented.".format(
                    row["ReferenceType"]))

            if seqs is not None:
                seq = seqs.rows[row["ReferenceIndex"]]

                for i in range(seq["NumTracks"]):
                    index, = struct.unpack_from(">H", seq["TrackIndex"], i * 2)
                    track = tras.rows[index]
                    track_event = tevs.rows[track["EventIndex"]]
                    command = track_event["Command"]
                    k = 0
                    # print(command)
                    while (k < len(command)):
                        cmd,cmd_len, = struct.unpack_from(">HB", command, k)
                        k += 3
                        param_bytes, = struct.unpack_from(f">{cmd_len}s", command, k)
                        k += cmd_len
                        if cmd == 0:
                            k = len(command)
                            break
                        elif cmd == 0x07d0:
                            u1, = struct.unpack_from(">H", param_bytes, 0)
                            assert (u1 == 2)
                            syn_idx, = struct.unpack_from(">H", param_bytes, 2)
                            # print(syn_idx)
                            r_data = syns.rows[syn_idx]["ReferenceItems"]
                            a, wav_idx = struct.unpack(">HH", r_data)
                            if (a != 1):
                                continue

                            is_stream = wavs.rows[wav_idx]["Streaming"]
                            assert (wavs.rows[wav_idx]["EncodeType"] == 2)
                            wav_id = wavs.rows[wav_idx][
                                "MemoryAwbId"] if is_stream == 0 else wavs.rows[
                                    wav_idx]["StreamAwbId"]
                            enc = wavs.rows[wav_idx]["EncodeType"]
                            stream_awb_id = -1 if is_stream == 0 else wavs.rows[
                                wav_idx]["StreamAwbPortNo"]

                            name = name_map.get(
                                row["ReferenceIndex"],
                                "UNKNOWN-{}".format(row["ReferenceIndex"]))
                            if any(t.name == name for t in self.tracks):
                                name += '-{}'.format(wav_id + 1) if any(
                                    t.name == "{}-{}".format(name, wav_id)
                                    for t in self.tracks) else '-{}'.format(
                                        wav_id)
                            self.tracks.append(
                                track_t(row["ReferenceIndex"], name, wav_id,
                                        enc, is_stream, stream_awb_id))
                        elif cmd == 0x07d1:
                            start_offset, = struct.unpack_from(">I", param_bytes)
                            # print("Start offset:", start_offset)

            else:
                # extract all the wavs
                for index in range(len(tras.rows)):
                    track = tras.rows[index]
                    if track["EventIndex"] == 0xFFFF:
                        continue
                    track_event = tevs.rows[track["EventIndex"]]
                    command = track_event["Command"]
                    k = 0
                    # print(command)
                    while (k < len(command)):
                        cmd,cmd_len, = struct.unpack_from(">HB", command, k)
                        k += 3
                        param_bytes, = struct.unpack_from(f">{cmd_len}s", command, k)
                        k += cmd_len
                        if cmd == 0:
                            k = len(command)
                            break
                        elif cmd == 0x07d0:
                            u1, = struct.unpack_from(">H", param_bytes, 0)
                            assert (u1 == 2)
                            syn_idx, = struct.unpack_from(">H", param_bytes, 2)
                            r_data = syns.rows[syn_idx]["ReferenceItems"]
                            a, wav_idx = struct.unpack(">HH", r_data)
                            if (a != 1):
                                continue

                            is_stream = wavs.rows[wav_idx]["Streaming"]
                            assert (wavs.rows[wav_idx]["EncodeType"] == 2)
                            wav_id = wavs.rows[wav_idx][
                                "MemoryAwbId"] if is_stream == 0 else wavs.rows[
                                    wav_idx]["StreamAwbId"]
                            enc = wavs.rows[wav_idx]["EncodeType"]
                            stream_awb_id = -1 if is_stream == 0 else wavs.rows[
                                wav_idx]["StreamAwbPortNo"]

                            name = name_map.get(
                                row["ReferenceIndex"],
                                "UNKNOWN-{}".format(row["ReferenceIndex"]))
                            if any(t.name == name for t in self.tracks):
                                name += '-{}'.format(wav_id + 1) if any(
                                    t.name == "{}-{}".format(name, wav_id)
                                    for t in self.tracks) else '-{}'.format(
                                        wav_id)
                            self.tracks.append(
                                track_t(row["ReferenceIndex"], name, wav_id,
                                        enc, is_stream, stream_awb_id))
                        elif cmd == 0x07d1:
                            start_offset, = struct.unpack_from(">I", param_bytes)
                            # print("Start offset:", start_offset)


def align(n):

    def _align(number):
        return math.ceil(number / n) * n

    return _align


afs2_file_ent_t = T("afs2_file_ent_t", ("cue_id", "offset", "size"))


class AFSArchive(object):

    def __init__(self, file):
        buf = R(file)

        magic = buf.uint32_t()
        if magic != 0x41465332:
            raise ValueError("bad magic")

        version = buf.bytes(4)
        file_count = buf.le_uint32_t()
        self.alignment = buf.le_uint32_t()
        cue_id_size = version[2]
        # print("afs2:", file_count, "files in ar")
        # print("afs2: aligned to", self.alignment, "bytes")

        self.offset_size = version[1]
        self.offset_mask = int("FF" * self.offset_size, 16)
        # print("afs2: a file offset is", self.offset_size, "bytes")

        self.files = []
        self.create_file_entries(buf, file_count, cue_id_size,
                                 self.offset_size, self.offset_mask)
        self.src = buf

    def _struct_format(self, size):
        if size == 2:
            return "H"
        elif size == 4:
            return "I"
        else:
            raise ValueError(
                "Cannot deal with size {0} at this time".format(size))

    def create_file_entries(self, buf, file_count, cue_id_size, offset_size,
                            offset_mask):
        buf.seek(0x10)
        read_cue_ids = struct.Struct("<" + (self._struct_format(cue_id_size) *
                                            file_count))
        read_raw_offs = struct.Struct("<" + (self._struct_format(offset_size) *
                                             (file_count + 1)))

        # read all in one go
        cue_ids = buf.struct(read_cue_ids)
        raw_offs = buf.struct(read_raw_offs)
        # apply the mask
        unaligned_offs = tuple(map(lambda x: x & offset_mask, raw_offs))
        aligned_offs = tuple(map(align(self.alignment), unaligned_offs))
        offsets_for_length_calculating = unaligned_offs[1:]
        lengths = itertools.starmap(
            lambda my_offset, next_offset: next_offset - my_offset,
            zip(aligned_offs, offsets_for_length_calculating))

        self.files = list(
            itertools.starmap(afs2_file_ent_t,
                              zip(cue_ids, aligned_offs, lengths)))

    def file_data_for_cue_id(self, cue_id):
        for f in self.files:
            if f.cue_id == cue_id:
                return self.file_data(f)
        else:
            if self.files[0].cue_id == 0:
                return self.file_data(self.files[0])
            raise ValueError("id {0} not found in archive".format(cue_id))

    def file_data(self, ent):
        return self.src.bytes(ent.size, at=ent.offset)


def extract_acb(acb_file, target_dir, acb_file_path):
    utf = UTFTable(acb_file)
    cue = TrackList(utf)
    embedded_awb = io.BytesIO(utf.rows[0]["AwbFile"])
    if embedded_awb.getbuffer().nbytes != 0:
        embedded_awb = AFSArchive(embedded_awb)
    external_awbs = []
    try:
        stream_awb_hash = UTFTable(io.BytesIO(utf.rows[0]['StreamAwbHash']))
        for awb in stream_awb_hash.rows:
            try_path = os.path.join(os.path.dirname(acb_file_path),
                                    "{}.awb".format(awb["Name"]))
            if os.path.exists(try_path):
                external_awbs.append(
                    AFSArchive(io.BytesIO(open(try_path, 'rb').read())))
    except ValueError:
        pass

    outputs = []
    for track in cue.tracks:
        name = "{0}{1}".format(
            track.name, wave_type_ftable.get(track.enc_type, track.enc_type))
        with open(os.path.join(target_dir, name), "wb") as named_out_file:
            if track.is_stream == 0:
                data_source = embedded_awb
            else:
                data_source = external_awbs[track.stream_awb_id]
            named_out_file.write(data_source.file_data_for_cue_id(
                track.wav_id))
        outputs.append(os.path.join(target_dir, name))

    return outputs


def main(invocation, acb_file, target_dir, *_):
    with open(acb_file, "rb") as acb:
        extract_acb(acb, target_dir, acb_file)


if __name__ == '__main__':
    main(*sys.argv)
