#!/usr/bin/env python3
"""Debug: trace 0x1054→0x1052 hierarchy and 0x1001 offsets."""
import sys
from ptx_parser import _decrypt, _parse_block_at, _index_blocks, _read4, _read8, _read2

PTF = '/private/var/www/pt2logic/8 - 27 - 08/Macintosh HD/Users/erikwidman/Desktop/blacklodge session/8 - 27 - 08/8 - 27 - 08.ptf'

with open(PTF, 'rb') as f:
    raw = f.read()
data = _decrypt(raw)
big_endian = (data[0x11] == 0x01)

blocks = []
pos = 0x14
while pos < len(data):
    nz = data.find(0x5A, pos)
    if nz == -1: break
    b, pos = _parse_block_at(data, nz, len(data), big_endian)
    if b is not None: blocks.append(b)
    else: pos = nz + 1

idx = _index_blocks(blocks)

# Collect 0x1014 track names in file order
track_names_ordered = []
for b in sorted(idx.get(0x1014, []), key=lambda b: b.offset):
    p = b.offset + 2
    if p + 4 > len(data): continue
    name_len = _read4(data, p, big_endian)
    if 0 < name_len < 200:
        name = data[p+4:p+4+name_len].decode('latin-1', errors='replace')
        track_names_ordered.append(name)
print(f"Track names in order ({len(track_names_ordered)}): {track_names_ordered}")

# ── 0x1054 → 0x1052 (tracks in order, direct children) ──────────────────────
print("\n=== 0x1054 containers → 0x1052 children IN ORDER ===")
for b54 in sorted(idx.get(0x1054, []), key=lambda b: b.offset):
    # Direct children that are 0x1052
    children_52 = [c for c in b54.children if c.content_type == 0x1052]
    print(f"0x1054 at offset={b54.offset} has {len(children_52)} direct 0x1052 children")
    for ti, b52 in enumerate(children_52):
        track_name = track_names_ordered[ti] if ti < len(track_names_ordered) else f"<unknown {ti}>"
        all_4f = []
        for b50 in b52.children:
            if b50.content_type == 0x1050:
                for b4f in b50.children:
                    if b4f.content_type == 0x104f and b4f.offset + 6 <= len(data):
                        rid = _read4(data, b4f.offset + 2, big_endian) >> 16
                        all_4f.append(rid)
        print(f"  track {ti}: {repr(track_name)}  regions={all_4f[:5]}{'...' if len(all_4f)>5 else ''} ({len(all_4f)} clips)")

# ── 0x1001 audio lengths: try different offsets ───────────────────────────────
print("\n=== 0x1001 blocks: testing offsets for length ===")
sample_rate = 96000
for i, b in enumerate(idx.get(0x1001, [])[:5]):
    print(f"  block i={i} size={b.block_size}")
    for off in (2, 4, 6, 8):
        if b.offset + off + 8 <= len(data):
            v = _read8(data, b.offset + off, big_endian)
            secs = v / sample_rate if v < 10**12 else float('inf')
            print(f"    @+{off}: {v}  ({secs:.2f}s)")

# ── 0x103a audio names: first block detail ────────────────────────────────────
print("\n=== 0x103a (audio names): raw first block ===")
for b in sorted(idx.get(0x103a, []), key=lambda b: b.offset)[:1]:
    print(f"block offset={b.offset} block_size={b.block_size}")
    # Dump raw content bytes
    print(f"  bytes 0-20 from b.offset: {data[b.offset:b.offset+21].hex()}")
    # Try parsing at b.offset+11 as the code does
    p = b.offset + 11
    for entry_i in range(5):
        if p + 4 > len(data): break
        name_len = _read4(data, p, big_endian)
        print(f"  entry {entry_i}: p={p} name_len={name_len}")
        if 0 < name_len < 200 and p + 4 + name_len <= len(data):
            name = data[p+4:p+4+name_len].decode('latin-1', errors='replace')
            p2 = p + 4 + name_len
            wavbytes = data[p2:p2+4].hex() if p2+4 <= len(data) else 'N/A'
            print(f"    name={repr(name)} wavbytes={wavbytes}")
            p = p2 + 4 + 5  # skip wavtype(4) + 5 metadata
        else:
            print(f"    skipping (bad name_len)")
            break
