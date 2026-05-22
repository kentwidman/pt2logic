# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Does

Converts Pro Tools session files (`.ptf` for PT7–8 era, `.ptx` for PT10+) to AAF files importable into Logic Pro X via **File → Import → AAF**. Preserves track layout, clip timeline positions, and pre-rendered fades. Does not transfer effects, automation, pan, or volume.

## Setup & Usage

```bash
pip3 install pyaaf2
python3 convert.py <session.ptf> output.aaf [--verbose]
```

## Architecture

Three-module pipeline with clean separation:

**`ptx_parser.py`** → **`aaf_writer.py`**, orchestrated by **`convert.py`**

### ptx_parser.py
Reads the binary `.ptf`/`.ptx` file and returns a `SessionData` dataclass. Pipeline:
1. XOR-decrypt the file (key derived from bytes `0x12`–`0x13`; PT5–9 uses `mul=53`, PT10+ uses `mul=11`)
2. Walk the block tree (each block: `0x5A` marker + 2-byte type + 4-byte size + 2-byte content_type)
3. Index all blocks by `content_type`, then extract:
   - Sample rate (`0x1028`)
   - Audio files (`0x103a` names, `0x1001` lengths) — includes Fade Files
   - Regions (`0x1008` for PT<10, `0x2629` for PT10+) via `parse_three_point()` variable-length encoding
   - Track placements (`0x100e` blocks referencing `0x1014` track names)
4. Resolve audio file paths relative to the session folder; set `af.is_fade = True` when "Fade Files" appears in the path

Key fragility: `parse_three_point()` uses nibble-encoded variable-length integers. Block size arithmetic (7-byte header + `block_size` bytes total span) must be exact.

### aaf_writer.py
Takes `SessionData` and writes a valid AAF using `pyaaf2`. Per-track pipeline:
1. `_trim_for_fades()` — trims main clips where fade files overlap (fade-in, fade-out, crossfade), so the final sequence has no overlapping audio
2. Builds a `Sequence` of `SourceClip` + `Filler` objects sorted by timeline position
3. Mob chain per audio file: `TapeMob (ImportDescriptor) → SourceMob (WAVEDescriptor/AIFCDescriptor) → MasterMob`; `CompositionMob` slots reference master mobs

Edit rate = session sample rate (e.g. `44100/1`) so all timing is in samples.

Per-track processing order in `write()`:
1. `_resolve_comp_overlaps()` — handles PT comp sessions where long continuous takes overlap shorter comped clips. Sorts clips by length ascending (shorter = higher priority), then for each clip fills only unclaimed time intervals, splitting long takes into segments around higher-priority clips.
2. `_trim_for_fades()` — trims main clips where pre-rendered fade files overlap.
3. Build `Sequence` of `SourceClip` + `Filler` objects.

### Data model (shared)
```
SessionData
  audio_files: [AudioFile(index, filename, length, resolved_path, is_fade)]
  regions:     [Region(index, name, audio_file, start_pos, sample_offset, length)]
  placements:  [TrackPlacement(track_name, track_index, region)]
```
`Region.start_pos` is the absolute timeline position in samples. `sample_offset` is the in-point within the source file.

## Supported Versions

- **Primary**: PT7–8 `.ptf` (2008 era) — `xor_type=0x01`, block types `0x1008`/`0x100b`
- **Secondary**: PT10–12 `.ptx` — `xor_type=0x05`, block types `0x2629`/`0x262a`
- PT13+ / PT2018+ not handled

## Key Dependencies

- `pyaaf2` — AAF read/write. Use `f.create.WAVEDescriptor()`, `f.create.SourceMob()`, etc. via the file's factory. `WAVEDescriptor['Summary']` requires raw RIFF bytes (use `aaf2.ama.get_wave_fmt(path)`); synthesize a placeholder when the file is unavailable.
- No C++ dependencies — pure Python

## Test Session

Working test file (PT7, 96000 Hz, 8 tracks, 71 audio files):
```
8 - 27 - 08/Macintosh HD/Users/erikwidman/Desktop/blacklodge session/8 - 27 - 08/8 - 27 - 08.ptf
```
Note: The top-level `8 - 27 - 08/8 - 27 - 08.ptf` is 0 bytes (data is in a macOS HFS+ resource fork). Use the nested path above.

Current output: 34 warnings — 33 expected "displaced clip" skips (cross-track alternate takes) + 1 long take fully covered by comped clips. Zero overlap errors.

## Remaining Work

- [ ] **End-to-end Logic Pro import test** — import `output.aaf` into Logic Pro X and verify tracks play back correctly (timeline positions, audio file references, fades).
- [ ] **PT10+ (.ptx) support validation** — the `.ptx` path (`0x2629` regions, `xor_type=0x05`) is implemented but untested. Find a PT10–12 session and run through it.
- [ ] **AIFF file handling** — `AIFCDescriptor` path uses `aaf2.ama.get_aifc_fmt()`; needs a session with `.aif` source files to confirm the summary bytes are accepted by Logic.
- [ ] **Displaced-clip heuristic review** — 33 displaced clips are silently skipped. Some may be legitimate alternate takes that belong on the timeline. Consider whether a `--keep-alternates` flag or separate output track would be useful.
- [ ] **PT13+ support** — not handled; would require reverse-engineering updated block types.
