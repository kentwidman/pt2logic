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
    is_fade: bool = False


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


AUDIO_EXTENSIONS = ('.wav', '.aif', '.aiff', '.bwf', '.w64')


def _get_wav_nsamples(path: str) -> int:
    """Return sample count for a WAV/BWF file by scanning RIFF chunks."""
    try:
        with open(path, 'rb') as f:
            header = f.read(12)
            if len(header) < 12 or header[:4] != b'RIFF' or header[8:12] != b'WAVE':
                return 0
            channels, bpf = 1, 2
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                chunk_id = hdr[:4]
                chunk_size = struct.unpack_from('<I', hdr, 4)[0]
                padded = chunk_size + (chunk_size % 2)
                if chunk_id == b'fmt ':
                    chunk = f.read(padded)
                    if len(chunk) >= 16:
                        channels = struct.unpack_from('<H', chunk, 2)[0]
                        bit_depth = struct.unpack_from('<H', chunk, 14)[0]
                        bpf = max(1, channels * (bit_depth // 8))
                elif chunk_id == b'data':
                    return chunk_size // bpf
                else:
                    f.seek(padded, 1)
    except OSError:
        pass
    return 0


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
    if pos >= max_end:
        return None, pos           # signal caller we're done — no advancement
    if pos + 9 > len(data):
        return None, pos + 1       # not enough room for a header, skip this byte
    if data[pos] != 0x5A:
        return None, pos + 1

    block_type = _read2(data, pos + 1, big_endian)
    block_size = _read4(data, pos + 3, big_endian)
    content_type = _read2(data, pos + 7, big_endian)

    # total block span: 7-byte header + block_size (which includes the 2-byte content_type)
    block_end = pos + 7 + block_size

    # Reject blocks whose declared size extends beyond the file — these are
    # false-positive 0x5A bytes with random bytes forming an unrealistic size.
    if block_end > len(data):
        return None, pos + 1

    content_offset = pos + 9
    scope_end = min(block_end, max_end)

    block = Block(
        block_type=block_type,
        block_size=block_size,
        content_type=content_type,
        offset=pos + 7,  # matches ptformat: points to content_type field, not past it
    )

    # Scan for child blocks within this block's content area.
    # Use bytes.find() to jump directly to the next 0x5A instead of advancing
    # byte-by-byte, which turns an O(n²) scan into O(n).
    child_pos = content_offset
    while child_pos < scope_end:
        next_z = data.find(0x5A, child_pos, scope_end)
        if next_z == -1:
            break
        child, child_pos = _parse_block_at(data, next_z, scope_end, big_endian)
        if child is not None:
            block.children.append(child)
        else:
            child_pos = next_z + 1   # false positive, skip past it

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

    if len(raw) < 4 or raw[0] != 0x03:
        raise ParseError("Not a Pro Tools session file (missing magic byte 0x03 at offset 0)")

    data = _decrypt(raw)

    # Detect endianness: ptformat convention — byte 0x11 == 0x01 means big-endian
    big_endian = (data[0x11] == 0x01) if len(data) > 0x11 else False

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

    # Parse block tree (ptformat convention: start at 0x1f)
    blocks = []
    pos = 0x1f
    while pos < len(data):
        next_z = data.find(0x5A, pos)
        if next_z == -1:
            break
        block, pos = _parse_block_at(data, next_z, len(data), big_endian)
        if block is not None:
            blocks.append(block)
        else:
            pos = next_z + 1

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
                if filename.lower().endswith(AUDIO_EXTENSIONS):
                    af = AudioFile(
                        index=len(audio_files),
                        filename=filename,
                        length=audio_lengths.get(i, 0),
                    )
                    audio_files.append(af)
                i += 1

    if not audio_files:
        warnings.append("No audio files found in session")

    # Resolve paths and detect fade files
    session_dir = os.path.dirname(os.path.abspath(ptf_path))
    for af in audio_files:
        af.resolved_path = _resolve_audio_path(session_dir, af.filename)
        # PTF stores relative paths; "Fade Files" in the name or resolved path = pre-rendered fade
        path_hint = (af.resolved_path or '') + af.filename
        af.is_fade = 'fade files' in path_hint.lower()
        if af.resolved_path is None:
            warnings.append(f"Audio file not found on disk: {af.filename}")
        elif af.length == 0:
            af.length = _get_wav_nsamples(af.resolved_path)

    session.audio_files = audio_files

    # --- Regions ---
    regions = []
    if version < 10:
        container_type, child_type = 0x100b, 0x1008
    else:
        container_type, child_type = 0x262a, 0x2629

    if child_type in idx:
        for i, b in enumerate(sorted(idx[child_type], key=lambda b: b.offset)):
            p = b.offset + 11
            if p + 4 > len(data):
                continue
            name_len = _read4(data, p, big_endian)
            if name_len == 0 or p + 4 + name_len > len(data):
                continue
            p += 4
            name = data[p:p + name_len].decode('latin-1', errors='replace')
            p += name_len

            # Region time values are in raw samples; ratefactor does not apply here.
            sample_offset, length, start_pos = _parse_three_point(data, p, big_endian, 1.0)

            # audio file index: 4 bytes at block_size-13 from b.offset
            findex_pos = b.offset + b.block_size - 13
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

    # Build ordered track name list from 0x1014 blocks (sorted by file offset = session order)
    track_names_ordered = []
    if 0x1014 in idx:
        for b in sorted(idx[0x1014], key=lambda b: b.offset):
            p = b.offset + 2
            if p + 4 > len(data):
                continue
            name, name_len = _parse_string(data, p, big_endian)
            if name_len > 0:
                track_names_ordered.append(name)

    # Build region index
    region_by_index = {r.index: r for r in regions}

    # Walk 0x1054 → 0x1052 (tracks) → 0x1050 → 0x104f (placements)
    # PT7/8 uses this hierarchy; each 0x1052 is one track in order.
    # Per ptformat: region index is a plain 4-byte LE value at b4f.offset+4;
    # timeline position is at b4f.offset+9 (4 bytes + 1 unknown byte after index).
    # 0x1050 blocks with byte +46 == 0x01 are fade markers — skip them here since
    # pre-rendered fades are handled separately via 0x100a blocks.
    found_placements = False
    if 0x1054 in idx:
        for b54 in sorted(idx[0x1054], key=lambda b: b.offset):
            children_52 = [c for c in b54.children if c.content_type == 0x1052]
            if not children_52:
                continue
            for ti, b52 in enumerate(children_52):
                track_name = track_names_ordered[ti] if ti < len(track_names_ordered) else f"Track {ti}"
                for b50 in b52.children:
                    if b50.content_type != 0x1050:
                        continue
                    # Skip 0x1050 blocks flagged as fade markers (ptformat offset +46 == 0x01)
                    if b50.offset + 48 <= len(data) and data[b50.offset + 46] == 0x01:
                        continue
                    for b4f in b50.children:
                        if b4f.content_type != 0x104f:
                            continue
                        if b4f.offset + 13 > len(data):
                            continue
                        region_idx = _read4(data, b4f.offset + 4, big_endian)
                        region = region_by_index.get(region_idx)
                        if region is None:
                            warnings.append(
                                f"Track '{track_name}': no region at index {region_idx}, skipping"
                            )
                            continue
                        # Timeline position at b4f.offset+9 (verified against ptformat).
                        tl_start = _read4(data, b4f.offset + 9, big_endian)
                        # Skip "displaced" clips: clips whose region was originally defined at a
                        # different timeline position than where they are placed here. These are
                        # alternate takes or cross-track edits that were superseded in the final
                        # session state. Clips at their native position (start_pos == tl_start)
                        # represent the active comp.
                        if region.start_pos != tl_start:
                            warnings.append(
                                f"Track '{track_name}': skipping displaced clip '{region.name}' "
                                f"(native pos={region.start_pos}, placed at={tl_start})"
                            )
                            continue
                        placed = Region(
                            index=region.index,
                            name=region.name,
                            audio_file=region.audio_file,
                            start_pos=tl_start,
                            sample_offset=region.sample_offset,
                            length=region.length if region.length > 0 else region.audio_file.length,
                        )
                        placements.append(TrackPlacement(
                            track_name=track_name,
                            track_index=ti,
                            region=placed,
                        ))
            found_placements = True
            break  # use only the first 0x1054 with tracks

    if not found_placements:
        warnings.append("No track placement blocks (0x1054) found — track layout may be empty")

    # --- Fade placements (0x100a blocks) ---
    # Each 0x100a contains one 0x1008 child (the fade region, with name_len=0) and
    # up to two 0x1050→0x104f children referencing the adjacent regular clips.
    # The adjacent clip refs tell us which track the fade belongs to and where the
    # junction is. Fade position = later_clip_start − fade_length (fade-out style,
    # ending exactly at the junction so the incoming clip plays from its start).
    region_to_track: dict[int, tuple[str, int]] = {
        p.region.index: (p.track_name, p.track_index) for p in placements
    }

    if 0x100a in idx:
        fade_region_index = len(regions)  # start fresh indices after normal regions
        for b100a in sorted(idx[0x100a], key=lambda b: b.offset):
            b1008 = next((c for c in b100a.children if c.content_type == 0x1008), None)
            if not b1008:
                continue

            fp = b1008.offset + 11
            if fp + 4 > len(data):
                continue
            name_len = _read4(data, fp, big_endian)
            if name_len > 200:
                continue
            fp += 4 + name_len  # skip name_len field (name is always empty for fades)

            sample_offset, fade_length, start_pos = _parse_three_point(data, fp, big_endian, 1.0)
            findex_pos = b1008.offset + b1008.block_size - 13
            findex = _read4(data, findex_pos, big_endian) if findex_pos + 4 <= len(data) else -1

            if not (0 <= findex < len(audio_files)):
                continue
            af = audio_files[findex]
            if not af.is_fade:
                continue

            fade_length = fade_length if fade_length > 0 else af.length
            if fade_length <= 0:
                continue

            # Collect adjacent clip refs, sorted by timeline position
            refs: list[tuple[int, int]] = []
            for b1050 in b100a.children:
                if b1050.content_type != 0x1050:
                    continue
                for b4f in b1050.children:
                    if b4f.content_type != 0x104f:
                        continue
                    region_ref = _read4(data, b4f.offset + 4, big_endian)
                    tl = _read4(data, b4f.offset + 9, big_endian) if b4f.offset + 13 <= len(data) else 0
                    refs.append((region_ref, tl))

            if not refs:
                continue
            refs.sort(key=lambda x: x[1])

            # Track: use the track of the first recognisable adjacent clip
            track_name, track_index = None, None
            for region_ref, _ in refs:
                if region_ref in region_to_track:
                    track_name, track_index = region_to_track[region_ref]
                    break
            if track_name is None:
                # Fallback: match region name prefix to a track name
                # e.g. 'Guitar 1 ribbon_02-02' → track 'Guitar 1 ribbon'
                track_by_name = {tn: ti for tn, ti in region_to_track.values()}
                for region_ref, _ in refs:
                    ref_region = region_by_index.get(region_ref)
                    if ref_region is None:
                        continue
                    rname_lower = ref_region.name.lower()
                    for tn, ti in track_by_name.items():
                        if rname_lower.startswith(tn.lower()):
                            track_name, track_index = tn, ti
                            break
                    if track_name is not None:
                        break

            if track_name is None:
                warnings.append(f"Fade '{af.filename}': cannot determine track, skipping")
                continue

            # Timeline position of the fade
            if len(refs) >= 2:
                # Crossfade: fade ends at the junction (= later clip's start)
                fade_tl_start = refs[-1][1] - fade_length
            else:
                # Single clip: fade-out at end of clip, or fade-in at start
                ref_region = region_by_index.get(refs[0][0])
                if ref_region is not None and refs[0][1] + ref_region.length > refs[0][1]:
                    # Fade-out: place at end of the referenced clip
                    fade_tl_start = refs[0][1] + ref_region.length - fade_length
                else:
                    # Fallback: fade-in at clip start
                    fade_tl_start = refs[0][1]

            if fade_tl_start < 0:
                fade_tl_start = 0

            fade_region = Region(
                index=fade_region_index,
                name=af.filename,
                audio_file=af,
                start_pos=fade_tl_start,
                sample_offset=0,
                length=fade_length,
            )
            fade_region_index += 1
            placements.append(TrackPlacement(
                track_name=track_name,
                track_index=track_index,
                region=fade_region,
            ))

    session.placements = placements
    return session


def _resolve_audio_path(session_dir: str, filename: str) -> str | None:
    # PTF may store full relative paths; use basename for filesystem lookup
    basename = os.path.basename(filename)
    candidates = [
        os.path.join(session_dir, "Audio Files", basename),
        os.path.join(session_dir, "Fade Files", basename),
        os.path.join(session_dir, basename),
    ]
    # Also search any immediate subdirectory (catches renamed "Audio Files" and "Fade Files" folders)
    try:
        for entry in os.listdir(session_dir):
            entry_path = os.path.join(session_dir, entry)
            if os.path.isdir(entry_path):
                candidates.append(os.path.join(entry_path, basename))
    except OSError:
        pass
    seen: set[str] = set()
    for c in candidates:
        if c not in seen:
            seen.add(c)
            if os.path.exists(c):
                return c
    return None
