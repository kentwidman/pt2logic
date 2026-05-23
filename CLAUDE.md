# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Does

Converts Pro Tools session files (`.ptf` for PT7–8 era, `.ptx` for PT10+) to AAF files importable into Logic Pro X via **File → Import → AAF**. Preserves track layout, clip timeline positions, pre-rendered fades, mute state, and stereo L/R pair panning. Does not transfer effects, automation, mono pan, or volume.

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
   - Track placements via `0x1054 → 0x1052 → 0x1050 → 0x104f` hierarchy
4. Resolve audio file paths relative to the session folder; set `af.is_fade = True` when "Fade Files" appears in the path

Key fragility: `parse_three_point()` uses nibble-encoded variable-length integers. Block size arithmetic (7-byte header + `block_size` bytes total span) must be exact.

#### 0x1052 block layout (one per track)
```
content_type  2 bytes  0x1052
name_len      4 bytes  LE uint32
name          N bytes  latin-1 string
clip_count    4 bytes  LE uint32 (number of 0x1050 children)
[child blocks ...]     0x1050 clip containers
active_byte   1 byte   0x01 = active/unmuted, 0x00 = muted
```
Track name is read directly from offset `b52.offset + 2` — **do not** use the external `0x1014` block list, which is unreliable for correlation.  Muted tracks are included in `SessionData.muted_tracks` and written to AAF at zero gain.

The session may contain multiple `0x1054` blocks (the test file has three: 8-track and 30-track variants). Only the first with `0x1052` children is used — the others appear to be alternate-take metadata structures, not the active timeline.

### aaf_writer.py
Takes `SessionData` and writes a valid AAF using `pyaaf2`. Per-track pipeline:
1. `_trim_for_fades()` — trims main clips where fade files overlap (fade-in, fade-out, crossfade), so the final sequence has no overlapping audio
2. Builds a `Sequence` of `SourceClip` + `Filler` objects sorted by timeline position
3. Mob chain per audio file: `TapeMob (ImportDescriptor) → SourceMob (WAVEDescriptor/AIFCDescriptor) → MasterMob`; `CompositionMob` slots reference master mobs
4. Muted tracks: the `Sequence` is wrapped in a `MonoAudioGain` `OperationGroup` with `LEVEL = 0/1`, so the track imports into Logic at zero volume rather than being omitted

Edit rate = session sample rate (e.g. `44100/1`) so all timing is in samples.

Per-track processing order in `write()`:
1. `_resolve_comp_overlaps()` — handles PT comp sessions where long continuous takes overlap shorter comped clips. Sorts clips by length ascending (shorter = higher priority), then for each clip fills only unclaimed time intervals, splitting long takes into segments around higher-priority clips.
2. `_trim_for_fades()` — trims main clips where pre-rendered fade files overlap.
3. Build `Sequence` of `SourceClip` + `Filler` objects.
4. If track is in `session.stereo_sides`, wrap with `_wrap_pan(f, seq, ±1, 1)` (inner).
5. If track is in `session.muted_tracks`, wrap with `_wrap_gain(f, seg, 0, 1)` (outer).

#### MonoAudioGain / MonoAudioPan pyaaf2 API note
`pyaaf2` ships with no built-in operation/parameter defs. Register them once per file via `_register_audio_defs(f)` before use:
```
MonoAudioGain  OperationDef  AUID 9d2ea891-0968-11d3-8a38-0050040ef7d2
LEVEL          ParameterDef  AUID e4962321-2267-11d3-8a4c-0050040ef7d2  type=Rational
MonoAudioPan   OperationDef  AUID db5c9f25-1db9-11d4-8001-080036210804
PAN            ParameterDef  AUID e4962322-2267-11d3-8a4c-0050040ef7d2  type=Rational
```
Pan values: `-1/1` = full left, `1/1` = full right.  Operation groups nest: outer group wraps inner (pan inner, gain outer for muted stereo tracks).

### Data model (shared)
```
SessionData
  audio_files:   [AudioFile(index, filename, length, resolved_path, is_fade)]
  regions:       [Region(index, name, audio_file, start_pos, sample_offset, length)]
  placements:    [TrackPlacement(track_name, track_index, region)]
  muted_tracks:  set[str]   — track names where PT mute flag was 0x00
  stereo_sides:  dict[str, str]  — track_name → 'L' or 'R' (detected from .L/.R filename suffix)
```
`Region.start_pos` is the absolute timeline position in samples. `sample_offset` is the in-point within the source file.

Stereo pair detection: after placements are built, `ptx_parser` scans audio filenames. A track whose clips use only `*.L.wav`/`*_L.wav` files is flagged `'L'`; `*.R.wav`/`*_R.wav` → `'R'`. `aaf_writer` wraps those tracks in a `MonoAudioPan` OperationGroup (`-1/1` for L, `+1/1` for R).

## Supported Versions

- **Primary**: PT7–8 `.ptf` (2008 era) — `xor_type=0x01`, block types `0x1008`/`0x100b`
- **Secondary**: PT10–12 `.ptx` — `xor_type=0x05`, block types `0x2629`/`0x262a`
- PT13+ / PT2018+ not handled

## Key Dependencies

- `pyaaf2` — AAF read/write. Use `f.create.WAVEDescriptor()`, `f.create.SourceMob()`, etc. via the file's factory. `WAVEDescriptor['Summary']` requires raw RIFF bytes (use `aaf2.ama.get_wave_fmt(path)`); synthesize a placeholder when the file is unavailable.
- No C++ dependencies — pure Python

## Test Session

Primary test file (PT7, 44100 Hz, 46 tracks, 692 audio files):
```
circa/circa mix4.ptf
```

Current output: 349 warnings — 345 displaced-clip skips (cross-track alternate takes + inactive-playlist clips) + 2 audio files not found on disk (`snare 1.wav`, `snare 2.wav`, using synthetic WAVE headers) + 2 corresponding "not found" log lines. Zero overlap errors. No muted tracks in this session (all `0x01` tail bytes). No stereo L/R pairs detected.

Legacy test file (PT7, 96000 Hz, 8 tracks, 71 audio files) — still present but no longer primary:
```
8 - 27 - 08/Macintosh HD/Users/erikwidman/Desktop/blacklodge session/8 - 27 - 08/8 - 27 - 08.ptf
```
Note: The top-level `8 - 27 - 08/8 - 27 - 08.ptf` is 0 bytes (data is in a macOS HFS+ resource fork). Use the nested path above.

## Remaining Work

- [ ] **End-to-end Logic Pro import test** — import `output.aaf` into Logic Pro X and verify tracks play back correctly (timeline positions, audio file references, fades, muted tracks at zero volume).
- [ ] **Mute flag validation on a session with muted tracks** — the `active_byte` theory (`0x00` = muted) is inferred from the test session where all tracks are `0x01`. Needs a session with known-muted tracks to confirm. If wrong, muted-track logic would need revisiting.
- [ ] **Mono track pan and volume** — automation block structure is partially decoded. `0x101c → 0x1023 → 0x1029` hierarchy: each track has 11 automation lanes; **lane[0] = volume** (confirmed by varied negative values in `v0` of 0x1029 across mix tracks), **lane[1] = pan** (one data point: v0=-120 for a panned track, but scale is unclear — not simply ±100). All 0x1025 value blocks are zeros in both test sessions so the static value lives in the 0x1029 payload at offset+3 as a signed BE int32. Needs a session with a known pan position to confirm the scale and write production code. `MonoAudioPan` infrastructure is already registered in `aaf_writer.py`.
- [ ] **PT10+ (.ptx) support validation** — the `.ptx` path (`0x2629` regions, `xor_type=0x05`) is implemented but untested. Find a PT10–12 session and run through it.
- [ ] **AIFF file handling** — `AIFCDescriptor` path uses `aaf2.ama.get_aifc_fmt()`; needs a session with `.aif` source files to confirm the summary bytes are accepted by Logic.
- [ ] **Displaced-clip heuristic review** — displaced clips are silently skipped. Some may be legitimate alternate takes. Consider a `--keep-alternates` flag.
- [ ] **PT13+ support** — not handled; would require reverse-engineering updated block types.
