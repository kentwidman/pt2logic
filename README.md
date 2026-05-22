# pt2logic

Convert Pro Tools session files to Logic Pro X via AAF.

Preserves track layout, clip positions, which takes were used, and pre-rendered fades. Does not transfer effects, automation, pan, or volume.

## Requirements

- Python 3.10+
- Logic Pro X

```bash
pip3 install pyaaf2
```

## Usage

```bash
python3 convert.py <session.ptf> output.aaf
```

Then in Logic Pro X: **File → Import → AAF** → select your `.aaf` file.

### Options

```
python3 convert.py session.ptf output.aaf --verbose
```

`--verbose` prints warnings about missing audio files or skipped clips.

## Supported Pro Tools Versions

| Version | File Format | Era |
|---|---|---|
| Pro Tools 7–8 | `.ptf` | ~2008 (primary target) |
| Pro Tools 9 | `.ptf` | 2010 |
| Pro Tools 10–12 | `.ptx` | 2011–2015 |

## What Transfers

| ✓ Transfers | ✗ Does Not Transfer |
|---|---|
| Track names and order | Effects / plugins |
| Clip timeline positions | Automation |
| Which takes were used | Pan and volume |
| Fade-ins and fade-outs | MIDI tracks |
| Crossfades | |

Fades are handled by using Pro Tools' pre-rendered fade audio files from the `Fade Files/` folder inside your session directory — no fade curve reconstruction needed.

## Audio File Paths

Audio files must be accessible on disk for Logic to play them back after import. The script searches for them in the standard Pro Tools session folder layout:

```
My Session/
├── My Session.ptf
├── Audio Files/
│   ├── Kick_01.wav
│   └── Snare_01.wav
└── Fade Files/
    └── Fade 0001.wav
```

If audio files are not found, the AAF still imports and the track layout is correct — clips will just show as offline in Logic until you relink them.

## Example Output

```
Parsing MySong.ptf ...
  PT version : 8
  Sample rate: 44100 Hz
  Audio files: 24
  Regions    : 87
  Placements : 112
  Tracks     : 16
    Acoustic Guitar (6 clips)
    Bass DI (8 clips)
    Kick (14 clips)
    ...

Writing MySong.aaf ...
Done. Import into Logic Pro X:
  File → Import → AAF → MySong.aaf
```
