# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Does

Converts Pro Tools session files (`.ptf` for PT7–8 era, `.ptx` for PT10+) to AAF files importable into Logic Pro X via **File → Import → AAF**. Preserves track layout, clip timeline positions, pre-rendered fades, mute state, stereo L/R pair panning, and static fader volume/pan for named tracks.

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
   - Automation (volume/pan) via `0x101c → 0x1023 → 0x1029` hierarchy
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

The session may contain multiple `0x1054` blocks. Only the first with `0x1052` children is used — the others appear to be alternate-take metadata structures, not the active timeline.

#### 0x1050 clip container — inactive-playlist flag
Each `0x1050` holds exactly one `0x104f` placement. There is one trailing byte after the `0x104f` block ends (within the `0x1050`'s declared size). When that byte is `0x01` the clip is on an inactive/alternate playlist and must be skipped, even when the `0x104f` byte at `+17` reads `0x03` (active). The `0x104f` byte-at-`+17` filter (`0x01` = alternate take) catches most such clips, but a small number slip through with `+17=0x03` and trailing `0x01` — both checks are required.

#### Automation block layout (0x101c)
```
0x101c
  └─ 0x102d
       └─ 0x101b  — track name (4-byte BE length prefix, then latin-1 string at +2)
  └─ 0x1023 [lane 0]  — volume lane
       └─ 0x1029  — static value: signed int32 at offset+3 (raw units; divide by 10 → dB)
  └─ 0x1023 [lane 1]  — pan lane
       └─ 0x1029  — static value: signed int32 at offset+3 (raw units; divide by 1000 → −1..+1)
  └─ 0x1023 [lanes 2–10]  — send levels/pans (not currently used)
```
Tracks named `"Audio N"` in the automation blocks are skipped — their names are too generic to match reliably to 0x1052 tracks. Only tracks with explicit names get vol/pan. Automation names may carry a `.dup1` / `.dup2` suffix; these are indexed both as-is and with the suffix stripped so `"guitar choppy.dup1"` also matches placement track `"guitar choppy"`.

Aux/send/master tracks (`drum mix`, `pdly`, `verb`, `sn verb`, `Master 1`, etc.) appear in automation but have no 0x1052 placement block; they are silently ignored.

**Volume scale:** raw / 10.0 → dB offset from unity (0 = no change, −1440 ≈ silent). Confirmed consistent across both test sessions.
**Pan scale:** raw / 1000.0 → −1..+1 normalised. **Unconfirmed** — all panned tracks in the test session are negative (left). Need a session with a known right-pan to verify sign and divisor. If wrong, adjust `pan = pan_raw / 1000.0` in `_extract_automation()`.

### aaf_writer.py
Takes `SessionData` and writes a valid AAF using `pyaaf2`. Per-track pipeline:
1. `_resolve_comp_overlaps()` — handles PT comp sessions where long continuous takes overlap shorter comped clips. Sorts clips by length ascending (shorter = higher priority), then for each clip fills only unclaimed time intervals, splitting long takes into segments around higher-priority clips.
2. `_trim_for_fades()` — trims main clips where pre-rendered fade files overlap (fade-in, fade-out, crossfade), so the final sequence has no overlapping audio.
3. Build `Sequence` of `SourceClip` + `Filler` objects sorted by timeline position.
4. If track is in `session.stereo_sides`, wrap with `_wrap_pan(f, seq, ±1, 1)` (inner).
   Else if `session.track_automation` has a non-zero pan, wrap with `_wrap_pan` using the computed rational.
5. If track is in `session.muted_tracks`, wrap with `_wrap_gain(f, seg, 0, 1)` (outer).
   Else if `session.track_automation` has a non-zero vol_db, wrap with `_wrap_gain` using linear gain as a rational.

Mob chain per audio file: `TapeMob (ImportDescriptor) → SourceMob (WAVEDescriptor/AIFCDescriptor) → MasterMob`; `CompositionMob` slots reference master mobs.

Edit rate = session sample rate (e.g. `44100/1`) so all timing is in samples.

#### MonoAudioGain / MonoAudioPan pyaaf2 API note
`pyaaf2` ships with no built-in operation/parameter defs. Register them once per file via `_register_audio_defs(f)` before use:
```
MonoAudioGain  OperationDef  AUID 9d2ea894-0968-11d3-8a38-0050040ef7d2  (OperationDef_MonoAudioGain)
LEVEL          ParameterDef  AUID e4962320-2267-11d3-8a4c-0050040ef7d2  type=Rational  (ParameterDef_Level)
MonoAudioPan   OperationDef  AUID 9d2ea893-0968-11d3-8a38-0050040ef7d2  (OperationDef_MonoAudioPan)
PAN            ParameterDef  AUID e4962322-2267-11d3-8a4c-0050040ef7d2  type=Rational  (ParameterDef_Pan)
```
Previous code used wrong AUIDs: gain `9d2ea891` = `OperationDef_VideoRepeat`; pan `db5c9f25...` not in standard model; level `e4962321` = `ParameterDef_Amplitude`. Logic silently ignored all of them.
Pan values: `-1/1` = full left, `1/1` = full right. Operation groups nest: pan inner, gain outer.

**Critical:** `f.create.OperationGroup(name, ...)` defaults to `DataDef_Picture` (video) unless `media_kind='sound'` is passed explicitly. Logic Pro silently ignores any OperationGroup with `DataDef_Picture` on an audio track. Always pass `media_kind='sound'` to both `_wrap_gain` and `_wrap_pan`.

### Data model (shared)
```
SessionData
  audio_files:     [AudioFile(index, filename, length, resolved_path, is_fade)]
  regions:         [Region(index, name, audio_file, start_pos, sample_offset, length)]
  placements:      [TrackPlacement(track_name, track_index, region)]
  muted_tracks:    set[str]   — track names where PT mute flag was 0x00
  stereo_sides:    dict[str, str]  — track_name → 'L' or 'R' (detected from .L/.R filename suffix)
  track_automation: dict[str, tuple[float, float]]  — track_name → (vol_db, pan)
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

Current output: 349 warnings — 345 displaced-clip skips (cross-track alternate takes + inactive-playlist clips) + 2 audio files not found on disk (`snare 1.wav`, `snare 2.wav`, using synthetic WAVE headers) + 2 corresponding "not found" log lines. Zero overlap errors. No muted tracks in this session (all `0x01` tail bytes). No stereo L/R pairs detected. 21 tracks get vol/pan from automation (named tracks only).

Legacy test file (PT7, 96000 Hz, 8 tracks, 71 audio files) — still present but no longer primary:
```
8 - 27 - 08/Macintosh HD/Users/erikwidman/Desktop/blacklodge session/8 - 27 - 08/8 - 27 - 08.ptf
```
Note: The top-level `8 - 27 - 08/8 - 27 - 08.ptf` is 0 bytes (data is in a macOS HFS+ resource fork). Use the nested path above.

## Remaining Work

- [ ] **End-to-end Logic Pro import test** — import `output.aaf` into Logic Pro X and verify: timeline positions correct, audio plays back, fader volumes match PT, pan positions match PT, muted tracks at zero volume, fades sound correct.
- [ ] **Pan scale validation** — pan is written but the ÷1000 divisor is unconfirmed. The circa session has pans from −22 to −165 raw (all negative, so no right-pan to compare). Open a panned track in PT (e.g. `guitar choppy` raw=−120, `vocal_one_fix` raw=−165) and check the pan knob position. If the Logic pan knob reads a different percentage, adjust `pan = pan_raw / 1000.0` in `_extract_automation()`.
- [ ] **"Audio N" track automation** — drum, guitar, and bass tracks are named generically (`"Audio 1"` etc.) in the 0x101c automation blocks even though they have real names in 0x1052. This means those tracks currently get no volume offset in the AAF. A reliable correlation between 0x101c order and 0x1052 order has not been found. Investigate whether the two lists share a common ordering, or whether a parent block ties them together.
- [ ] **Mute flag validation** — the `active_byte` theory (`0x00` = muted) is inferred; no session with known-muted tracks has been tested. If Logic shows unmuted tracks, revisit the tail-byte logic in the 0x1052 parser.
- [ ] **PT10+ (.ptx) support validation** — the `.ptx` path (`0x2629` regions, `xor_type=0x05`) is implemented but untested. Find a PT10–12 session and run through it.
- [ ] **AIFF file handling** — `AIFCDescriptor` path uses `aaf2.ama.get_aifc_fmt()`; needs a session with `.aif` source files to confirm the summary bytes are accepted by Logic.
- [ ] **Displaced-clip heuristic review** — 345 clips are silently skipped per session. Some may be legitimate alternate takes that belong on the timeline. Consider a `--keep-alternates` flag.
- [ ] **PT13+ support** — not handled; would require reverse-engineering updated block types.
