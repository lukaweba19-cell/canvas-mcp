"""Build a real Word 97-2003 OLE document in memory.

Adapted from ``legacy-doc``'s own test fixture (MIT licensed,
https://github.com/lcorrigan704/legacy-doc), so the legacy Word path is exercised
against a genuine OLE compound file with a Word piece table instead of a stub.
"""

from __future__ import annotations

import struct
from math import ceil

END_OF_CHAIN = 0xFFFFFFFE
FREE_SECTOR = 0xFFFFFFFF
FAT_SECTOR = 0xFFFFFFFD
SECTOR_SIZE = 512


def build_doc(text: str = "Hello legacy doc", *, compressed: bool = False) -> bytes:
    """A minimal Word 97-2003 .doc whose text sits behind a real piece table."""
    flags = 0
    text_bytes = text.encode("cp1252") if compressed else text.encode("utf-16le")
    fc_min = 0x44
    fc_mac = fc_min + len(text_bytes)

    word_document = bytearray(fc_mac)
    struct.pack_into("<H", word_document, 0x0A, flags)
    struct.pack_into("<II", word_document, 0x18, fc_min, fc_mac)
    word_document[fc_min:fc_mac] = text_bytes

    char_count = len(text)
    fc_encoded = (fc_min * 2) | 0x40000000 if compressed else fc_min
    piece_table = (
        struct.pack("<II", 0, char_count)
        + b"\x00\x00"
        + struct.pack("<I", fc_encoded)
        + b"\x00\x00"
    )
    table_stream = b"\x02" + struct.pack("<I", len(piece_table)) + piece_table

    return _ole_file(
        {
            "WordDocument": bytes(word_document),
            "0Table": table_stream,
        }
    )


def _ole_file(streams: dict[str, bytes]) -> bytes:
    sector_payloads: list[bytes] = [b"", b""]
    stream_locations: dict[str, tuple[int, int]] = {}
    fat_entries = [END_OF_CHAIN, FAT_SECTOR]

    for name, payload in streams.items():
        start_sector = len(sector_payloads)
        sector_count = max(1, ceil(len(payload) / SECTOR_SIZE))
        stream_locations[name] = (start_sector, len(payload))
        padded = payload.ljust(sector_count * SECTOR_SIZE, b"\x00")
        for index in range(sector_count):
            sector_payloads.append(
                padded[index * SECTOR_SIZE : (index + 1) * SECTOR_SIZE]
            )
            current_sector = start_sector + index
            next_sector = (
                END_OF_CHAIN if index == sector_count - 1 else current_sector + 1
            )
            fat_entries.append(next_sector)

    directory = bytearray()
    directory.extend(_directory_entry("Root Entry", 5, END_OF_CHAIN, 0))
    for name, (start_sector, size) in stream_locations.items():
        directory.extend(_directory_entry(name, 2, start_sector, size))
    directory_payload = bytes(directory)
    directory_sector_count = max(1, ceil(len(directory_payload) / SECTOR_SIZE))
    directory_payload = directory_payload.ljust(
        directory_sector_count * SECTOR_SIZE,
        b"\x00",
    )
    sector_payloads[0] = directory_payload[:SECTOR_SIZE]
    if directory_sector_count > 1:
        first_extra_directory_sector = len(sector_payloads)
        fat_entries[0] = first_extra_directory_sector
        for index in range(1, directory_sector_count):
            sector_payloads.append(
                directory_payload[index * SECTOR_SIZE : (index + 1) * SECTOR_SIZE]
            )
            current_sector = first_extra_directory_sector + index - 1
            next_sector = (
                END_OF_CHAIN
                if index == directory_sector_count - 1
                else current_sector + 1
            )
            fat_entries.append(next_sector)
    sector_payloads[1] = struct.pack(
        "<128I",
        *fat_entries[:128],
        *([FREE_SECTOR] * (128 - len(fat_entries[:128]))),
    )

    header = bytearray(SECTOR_SIZE)
    header[:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", header, 0x1A, 3)
    struct.pack_into("<H", header, 0x1C, 0xFFFE)
    struct.pack_into("<H", header, 0x1E, 9)
    struct.pack_into("<H", header, 0x20, 6)
    struct.pack_into("<I", header, 0x2C, 1)
    struct.pack_into("<I", header, 0x30, 0)
    struct.pack_into("<I", header, 0x38, 0)
    struct.pack_into("<I", header, 0x3C, END_OF_CHAIN)
    struct.pack_into("<I", header, 0x40, 0)
    struct.pack_into("<I", header, 0x44, END_OF_CHAIN)
    struct.pack_into("<I", header, 0x48, 0)
    struct.pack_into("<I", header, 0x4C, 1)
    for offset in range(0x50, 0x4C + 109 * 4, 4):
        struct.pack_into("<I", header, offset, FREE_SECTOR)

    return bytes(header) + b"".join(sector_payloads)


def _directory_entry(name: str, object_type: int, start_sector: int, size: int) -> bytes:
    raw = bytearray(128)
    encoded_name = name.encode("utf-16le") + b"\x00\x00"
    raw[: len(encoded_name)] = encoded_name
    struct.pack_into("<H", raw, 64, len(encoded_name))
    raw[66] = object_type
    struct.pack_into("<III", raw, 68, FREE_SECTOR, FREE_SECTOR, FREE_SECTOR)
    struct.pack_into("<I", raw, 116, start_sector)
    struct.pack_into("<Q", raw, 120, size)
    return bytes(raw)
