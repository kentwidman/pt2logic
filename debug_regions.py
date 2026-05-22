#!/usr/bin/env python3
"""Debug: confirm findex offset in 0x1008 blocks with non-zero values."""
from ptx_parser import _decrypt, _parse_block_at, _index_blocks, _read4

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
regions_08 = sorted(idx.get(0x1008, []), key=lambda b: b.offset)

print(f"Total 0x1008 blocks: {len(regions_08)}")

# Print candidate findex values at different offsets from end
n_audio = 71
print(f"Audio file count: {n_audio}")
print(f"\n{'offset':>8}  {'bs':>4}  {'fi@-10':>8}  {'fi@-6':>8}  {'fi@-4':>8}  {'fi@-2':>8}  name")
for b in regions_08[:30]:
    bs = b.block_size
    p = b.offset + 11
    name_len = _read4(data, p, big_endian)
    name = data[p+4:p+4+name_len].decode('latin-1', errors='replace') if 0 < name_len < 80 else '?'

    def rv(off):
        a = b.offset + bs + off
        return _read4(data, a, big_endian) if a + 4 <= len(data) else -1

    fi10 = rv(-10)
    fi6  = rv(-6)
    fi4  = rv(-4)
    fi2  = rv(-2)

    # mark valid indices
    mark = lambda v: f"*{v:3d}*" if 0 <= v < n_audio else f"{v:8d}"
    print(f"{b.offset:>8}  {bs:>4}  {mark(fi10):>8}  {mark(fi6):>8}  {mark(fi4):>8}  {mark(fi2):>8}  {name}")
