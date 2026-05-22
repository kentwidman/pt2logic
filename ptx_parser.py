"""
Parser for Pro Tools session files (.ptf for PT5-9, .ptx for PT10+).
Primary target: PT7-8 .ptf files (2008 era), xor_type=0x01, mul=53.
Based on reverse engineering documented in https://github.com/zamaudio/ptformat
"""

import struct
import os
from dataclasses import dataclass, field


class PTXError(Exception):
    pass

class UnsupportedVersionError(PTXError):
    pass

class ParseError(PTXError):
    pass


@dataclass
class AudioFile:
    index: int
    filename: str
    length: int = 0
    resolved_path: str | None = None


@dataclass
class Region:
    index: int
    name: str
    audio_file: AudioFile
    start_pos: int      # absolute timeline position in samples
    sample_offset: int  # in-point within source file
    length: int         # duration in samples


@dataclass
class TrackPlacement:
    track_name: str
    track_index: int
    region: Region


@dataclass
class SessionData:
    sample_rate: int
    version: int
    audio_files: list = field(default_factory=list)
    regions: list = field(default_factory=list)
    placements: list = field(default_factory=list)


@dataclass
class Block:
    block_type: int
    block_size: int
    content_type: int
    offset: int          # byte offset of content start in data buffer
    children: list = field(default_factory=list)


VALID_AUDIO_TYPES = {b'WAVE', b'EVAW', b'AIFF', b'FFIA'}


def _gen_xor_delta(xor_value, mul, negative):
    for i in range(256):
        candidate = (i * mul) & 0xFF
        if negative:
            if (256 - candidate) & 0xFF == xor_value:
                return i
        else:
            if candidate == xor_value:
                return i
    raise ParseError(f"Cannot find XOR delta for xor_value=0x{xor_value:02x}")


def _decrypt(raw: bytes) -> bytes:
    raw = bytearray(raw)
    if len(raw) < 0x14:
        raise ParseError("File too small")

    xor_type = raw[0x12]
    xor_value = raw[0x13]

    if xor_type == 0x01:
        mul = 53
        negative = False
    elif xor_type == 0x05:
        mul = 11
        negative = True
    else:
        raise UnsupportedVersionError(f"Unknown xor_type: 0x{xor_type:02x}")

    delta = _gen_xor_delta(xor_value, mul, negative)
    xxor = bytes((i * delta) & 0xFF for i in range(256))

    result = bytearray(raw[:0x14])
    for i, byte in enumerate(raw[0x14:], start=0x14):
        if xor_type == 0x01:
            idx = i & 0xFF
        else:
            idx = (i >> 12) & 0xFF
        result.append(byte ^ xxor[idx])

    return bytes(result)


def _read2(data, offset, big_endian):
    fmt = '>H' if big_endian else '<H'
    return struct.unpack_from(fmt, data, offset)[0]


def _read4(data, offset, big_endian):
    fmt = '>I' if big_endian else '<I'
    return struct.unpack_from(fmt, data, offset)[0]


def _read8(data, offset, big_endian):
    fmt = '>Q' if big_endian else '<Q'
    return struct.unpack_from(fmt, data, offset)[0]


def _read_le_n(data, offset, n):
    if n == 0:
        return 0
    val = 0
    for i in range(n):
        val |= data[offset + i] << (8 * i)
    return val


def _parse_string(data, offset, big_endian):
    length = _read4(data, offset, big_endian)
    s = data[offset + 4: offset + 4 + length]
    return s.decode('latin-1', errors='replace'), length


def _parse_block_at(data, pos, max_end, big_endian):
    if pos >= max_end or pos + 9 > len(data):
        return None, pos
    if data[pos] != 0x5A:
        return None, pos + 1

    block_type = _read2(data, pos + 1, big_endian)
    block_size = _read4(data, pos + 3, big_endian)
    content_type = _read2(data, pos + 7, big_endian)

    # content starts at pos+9 (after the 9-byte header)
    content_offset = pos + 9
    # total block span: 7 bytes (header before size) + block_size bytes
    # block_size includes the 2-byte content_type field
    block_end = pos + 7 + block_size
    if block_end > len(data):
        block_end = len(data)

    block = Block(
        block_type=block_type,
        block_size=block_size,
        content_type=content_type,
        offset=content_offset,
    )

    child_pos = content_offset
    while child_pos < block_end and child_pos < max_end:
        child, child_pos = _parse_block_at(data, child_pos, block_end, big_endian)
        if child is not None:
            block.children.append(child)

    return block, block_end


def _index_blocks(blocks):
    index = {}
    stack = list(blocks)
    while stack:
        b = stack.pop()
        index.setdefault(b.content_type, []).append(b)
        stack.extend(b.children)
    return index


def _parse_three_point(data, pos, big_endian, ratefactor):
    if pos + 5 > len(data):
        return 0, 0, 0
    if not big_endian:
        offset_bytes = (data[pos + 1] >> 4) & 0xF
        length_bytes = (data[pos + 2] >> 4) & 0xF
        start_bytes  = (data[pos + 3] >> 4) & 0xF
    else:
        offset_bytes = (data[pos + 4] >> 4) & 0xF
        length_bytes = (data[pos + 3] >> 4) & 0xF
        start_bytes  = (data[pos + 2] >> 4) & 0xF

    p = pos + 5
    end = len(data)

    offset_val = _read_le_n(data, p, offset_bytes) if p + offset_bytes <= end else 0
    p += offset_bytes
    length_val = _read_le_n(data, p, length_bytes) if p + length_bytes <= end else 0
    p += length_bytes
    start_val  = _read_le_n(data, p, start_bytes)  if p + start_bytes  <= end else 0

    return (
        int(offset_val * ratefactor),
        int(length_val * ratefactor),
        int(start_val  * ratefactor),
    )


def parse(ptf_path: str, warnings: list | None = None) -> SessionData:
    if warnings is None:
        warnings = []

    with open(ptf_path, 'rb') as f:
        raw = f.read()

    if len(raw) < 4 or raw[3] != 0x03:
        raise ParseError("Not a Pro Tools session file (missing magic byte at 0x03)")

    data = _decrypt(raw)

    # Detect endianness
    big_endian = bool(data[0x11] & 0x01) if len(data) > 0x11 else False

    # Detect version
    version = None
    for offset in (0x40, 0x3D, 0x3A):
        if offset < len(data) and 5 <= data[offset] <= 12:
            version = data[offset]
            break
    if version is None:
        raise UnsupportedVersionError("Cannot determine Pro Tools version (expected 5-12)")

    # Determine ratefactor (default 1.0; refined after sample rate parse)
    # We'll pass 1.0 initially and fix up after parsing sample rate
    ratefactor = 1.0

    # Parse block tree
    blocks = []
    pos = 0x14
    while pos < len(data):
        block, pos = _parse_block_at(data, pos, len(data), big_endian)
        if block is not None:
            blocks.append(block)

    idx = _index_blocks(blocks)

    # --- Sample rate ---
    sample_rate = 44100  # default
    if 0x1028 in idx:
        b = idx[0x1028][0]
        if b.offset + 8 <= len(data):
            sample_rate = _read4(data, b.offset + 4, big_endian)

    ratefactor = sample_rate / 25.0

    session = SessionData(sample_rate=sample_rate, version=version)

    # --- Audio files ---
    audio_files = []
    audio_lengths = {}

    if 0x1001 in idx:
        for b in idx[0x1001]:
            p = b.offset + 8
            i = 0
            while p + 8 <= b.offset + b.block_size:
                audio_lengths[i] = _read8(data, p, big_endian) if p + 8 <= len(data) else 0
                p += 8
                i += 1

    if 0x103a in idx:
        for b in idx[0x103a]:
            p = b.offset + 11
            i = 0
            while p < b.offset + b.block_size - 2 and p < len(data):
                if p + 4 > len(data):
                    break
                name_len = _read4(data, p, big_endian)
                if name_len == 0 or p + 4 + name_len + 4 > len(data):
                    break
                p += 4
                filename = data[p:p + name_len].decode('latin-1', errors='replace')
                p += name_len
                wavtype = data[p:p + 4] if p + 4 <= len(data) else b''
                p += 4
                p += 5  # skip metadata bytes
                if wavtype in VALID_AUDIO_TYPES:
                    af = AudioFile(
                        index=len(audio_files),
                        filename=filename,
                        length=audio_lengths.get(i, 0),
                    )
                    audio_files.append(af)
                i += 1

    if not audio_files:
        warnings.append("No audio files found in session")

    # Resolve paths
    session_dir = os.path.dirname(os.path.abspath(ptf_path))
    for af in audio_files:
        af.resolved_path = _resolve_audio_path(session_dir, af.filename)
        if af.resolved_path is None:
            warnings.append(f"Audio file not found on disk: {af.filename}")

    session.audio_files = audio_files

    # --- Regions ---
    regions = []
    if version < 10:
        container_type, child_type = 0x100b, 0x1008
    else:
        container_type, child_type = 0x262a, 0x2629

    if child_type in idx:
        for i, b in enumerate(idx[child_type]):
            p = b.offset + 11
            if p + 4 > len(data):
                continue
            name_len = _read4(data, p, big_endian)
            if name_len == 0 or p + 4 + name_len > len(data):
                continue
            p += 4
            name = data[p:p + name_len].decode('latin-1', errors='replace')
            p += name_len + 4  # skip 4 bytes after name

            sample_offset, length, start_pos = _parse_three_point(data, p, big_endian, ratefactor)

            # audio file index at end of block
            findex_pos = b.offset + b.block_size
            if findex_pos + 4 <= len(data):
                findex = _read4(data, findex_pos, big_endian)
            else:
                findex = 0

            if findex >= len(audio_files):
                warnings.append(f"Region '{name}' references nonexistent audio file index {findex}, skipping")
                continue

            regions.append(Region(
                index=i,
                name=name,
                audio_file=audio_files[findex],
                start_pos=start_pos,
                sample_offset=sample_offset,
                length=length,
            ))
    else:
        warnings.append(f"No region block (content_type=0x{child_type:04x}) found")

    session.regions = regions

    # --- Track placements ---
    placements = []

    # Build track name lookup from 0x1015 → 0x1014
    track_names = {}
    if 0x1014 in idx:
        for b in idx[0x1014]:
            p = b.offset + 2
            if p + 4 > len(data):
                continue
            name, name_len = _parse_string(data, p, big_endian)
            p += 4 + name_len + 5
            if p + 4 > len(data):
                continue
            nch = _read4(data, p, big_endian)
            p += 4
            p += nch * 2
            # track index is block_type of the parent 0x1014 block
            track_idx = b.block_type
            track_names[track_idx] = name

    # Build region index
    region_by_index = {r.index: r for r in regions}

    # Walk 0x100e blocks for placements
    if 0x100e in idx:
        for b in idx[0x100e]:
            if b.offset + 8 > len(data):
                continue
            raw_track_idx = _read4(data, b.offset + 4, big_endian)
            track_name = track_names.get(raw_track_idx, f"Track {raw_track_idx}")

            # region index is in parent 0x100f block — find via block_type of this block
            region_idx = b.block_type
            region = region_by_index.get(region_idx)
            if region is None:
                continue

            placements.append(TrackPlacement(
                track_name=track_name,
                track_index=raw_track_idx,
                region=region,
            ))
    else:
        warnings.append("No track placement blocks (0x100e) found — track layout may be empty")

    session.placements = placements
    return session


def _resolve_audio_path(session_dir: str, filename: str) -> str | None:
    candidates = [
        os.path.join(session_dir, "Audio Files", filename),
        os.path.join(session_dir, filename),
    ]
    try:
        for entry in os.listdir(session_dir):
            if "Audio Files" in entry:
                candidates.append(os.path.join(session_dir, entry, filename))
    except OSError:
        pass
    for c in candidates:
        if os.path.exists(c):
            return c
    return None
