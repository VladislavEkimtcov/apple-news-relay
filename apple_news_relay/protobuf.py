"""A small, schema-less reader for the Protocol Buffers wire format.

Apple News stores its records as protobuf messages without shipping the
schema, so this module decodes the wire format generically and lets callers
ask for a field number as a string, an integer, a nested message and so on.
Only reading is supported.
"""

from __future__ import annotations

import struct
from typing import Dict, Iterator, List, Optional, Tuple, Union

VARINT, FIXED64, LENGTH, FIXED32 = 0, 1, 2, 5

Value = Union[int, bytes]


class DecodeError(ValueError):
    """Raised when bytes are not a well-formed protobuf message."""


def read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    """Read a base-128 varint at ``pos``; return ``(value, next_pos)``."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise DecodeError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise DecodeError("varint longer than 64 bits")


def iter_fields(buf: bytes) -> Iterator[Tuple[int, int, Value]]:
    """Yield ``(field_number, wire_type, value)`` for every field in ``buf``.

    Fixed-width values are returned as raw bytes; length-delimited values as
    bytes; varints as ints.
    """
    pos = 0
    end = len(buf)
    while pos < end:
        key, pos = read_varint(buf, pos)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise DecodeError("field number 0")
        if wire == VARINT:
            value, pos = read_varint(buf, pos)
        elif wire == FIXED64:
            value, pos = buf[pos:pos + 8], pos + 8
        elif wire == LENGTH:
            size, pos = read_varint(buf, pos)
            value, pos = buf[pos:pos + size], pos + size
        elif wire == FIXED32:
            value, pos = buf[pos:pos + 4], pos + 4
        else:
            raise DecodeError("unsupported wire type %d" % wire)
        if pos > end:
            raise DecodeError("field runs past the end of the message")
        yield field, wire, value


class Message:
    """A decoded message: field number -> list of raw values, with typed getters.

    Every getter takes a default and never raises for a missing field, so
    mapping code stays short and tolerant of schema drift between News
    versions.
    """

    __slots__ = ("fields",)

    def __init__(self, buf: bytes = b""):
        self.fields: Dict[int, List[Tuple[int, Value]]] = {}
        for field, wire, value in iter_fields(buf):
            self.fields.setdefault(field, []).append((wire, value))

    def __contains__(self, field: int) -> bool:
        return field in self.fields

    def raw(self, field: int) -> List[Tuple[int, Value]]:
        return self.fields.get(field, [])

    def int(self, field: int, default: Optional[int] = None) -> Optional[int]:
        for wire, value in self.raw(field):
            if wire == VARINT:
                return value  # type: ignore[return-value]
        return default

    def bool(self, field: int, default: bool = False) -> bool:
        value = self.int(field)
        return default if value is None else value != 0

    def bytes(self, field: int) -> Optional[bytes]:
        for wire, value in self.raw(field):
            if wire == LENGTH:
                return value  # type: ignore[return-value]
        return None

    def string(self, field: int, default: Optional[str] = None) -> Optional[str]:
        value = self.bytes(field)
        if value is None:
            return default
        return value.decode("utf-8", "replace")

    def strings(self, field: int) -> List[str]:
        return [v.decode("utf-8", "replace") for w, v in self.raw(field) if w == LENGTH]  # type: ignore[union-attr]

    def double(self, field: int, default: Optional[float] = None) -> Optional[float]:
        for wire, value in self.raw(field):
            if wire == FIXED64:
                return struct.unpack("<d", value)[0]  # type: ignore[arg-type]
        return default

    def message(self, field: int) -> Optional["Message"]:
        value = self.bytes(field)
        if value is None:
            return None
        try:
            return Message(value)
        except DecodeError:
            return None

    def messages(self, field: int) -> List["Message"]:
        out = []
        for wire, value in self.raw(field):
            if wire != LENGTH:
                continue
            try:
                out.append(Message(value))  # type: ignore[arg-type]
            except DecodeError:
                continue
        return out

    def timestamp(self, field: int) -> Optional[float]:
        """Apple's date encoding: one tag byte (0x01) followed by a little-endian
        double of seconds since 1970. Returns Unix seconds or None."""
        value = self.bytes(field)
        if value is None or len(value) != 9 or value[0] != 1:
            return None
        return struct.unpack("<d", value[1:])[0]


def encode_varint(value: int) -> bytes:
    """Encode an unsigned varint (used by tests to build fixtures)."""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def encode_field(field: int, value: Union[int, str, bytes, float, None], wire: Optional[int] = None) -> bytes:
    """Encode one field. Ints become varints, str/bytes length-delimited,
    floats fixed64 doubles. Used to build synthetic fixtures in tests."""
    if value is None:
        return b""
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int) and wire in (None, VARINT):
        return encode_varint(field << 3 | VARINT) + encode_varint(value)
    if isinstance(value, float):
        return encode_varint(field << 3 | FIXED64) + struct.pack("<d", value)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return encode_varint(field << 3 | LENGTH) + encode_varint(len(value)) + value  # type: ignore[arg-type]


def encode_timestamp(unix_seconds: float) -> bytes:
    """Inverse of :meth:`Message.timestamp`."""
    return b"\x01" + struct.pack("<d", unix_seconds)
