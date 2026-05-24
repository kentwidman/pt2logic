"""
Generates an AAF file from parsed Pro Tools session data.
The AAF can be imported into Logic Pro X via File → Import → AAF.
"""

import os
import struct
import pathlib
import sys
from fractions import Fraction

import aaf2
import aaf2.mobs
import aaf2.components
import aaf2.ama
from aaf2.rational import AAFRational
from aaf2.auid import AUID

# Standard AAF AUIDs for MonoAudioGain (from AAF SDK / pyaaf2 model)
_MONO_AUDIO_GAIN_AUID = AUID("9d2ea894-0968-11d3-8a38-0050040ef7d2")  # OperationDef_MonoAudioGain
_LEVEL_PARAM_AUID     = AUID("e4962320-2267-11d3-8a4c-0050040ef7d2")  # ParameterDef_Level

from ptx_parser import SessionData, AudioFile, Region, TrackPlacement


def _make_wave_summary(channels: int, sample_rate: int, bit_depth: int) -> bytes:
    """Build a minimal RIFF WAVE fmt chunk for WAVEDescriptor.Summary when file is unavailable."""
    byte_rate = sample_rate * channels * (bit_depth // 8)
    block_align = channels * (bit_depth // 8)
    fmt_chunk = struct.pack(
        '<4sI2H2I2H',
        b'fmt ',
        16,
        1,            # PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bit_depth,
    )
    data_size = 0
    riff_size = 4 + len(fmt_chunk) + 8  # 'WAVE' + fmt chunk + empty 'data' header
    riff_header = struct.pack('<4sI4s', b'RIFF', riff_size, b'WAVE')
    data_header = struct.pack('<4sI', b'data', data_size)
    return riff_header + fmt_chunk + data_header


def _get_audio_channels_and_depth(path: str) -> tuple[int, int]:
    try:
        with open(path, 'rb') as f:
            header = f.read(12)
            if len(header) < 12 or header[:4] != b'RIFF' or header[8:12] != b'WAVE':
                return 1, 24
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
                        return max(1, channels), max(8, bit_depth)
                    break
                else:
                    f.seek(padded, 1)
    except OSError:
        pass
    return 1, 24


def _trim_for_fades(placements: list, track_name: str, warnings: list) -> list:
    """
    Pre-rendered fade files overlap the ends/starts of adjacent main clips.
    Trim those main clips so the timeline has no overlapping audio.

    Fade-out:  main clip [====|fade|]  →  [====] + [fade]
    Fade-in:   main clip [|fade|====]  →  [fade] + [====]
    Crossfade: clip_A [===|xfade]  clip_B [xfade|===]  →  [===] [xfade] [===]
    """
    fades = [p for p in placements if p.region.audio_file.is_fade]
    if not fades:
        return placements

    mains = [p for p in placements if not p.region.audio_file.is_fade]
    result = []

    for p in mains:
        r = p.region
        clip_start = r.start_pos
        clip_end = r.start_pos + r.length
        trim_start = clip_start
        trim_end = clip_end

        for fp in fades:
            fr = fp.region
            fade_start = fr.start_pos
            fade_end = fr.start_pos + fr.length

            # Fade overlaps the end of this clip (fade-out or crossfade tail)
            if clip_start < fade_start < trim_end:
                trim_end = min(trim_end, fade_start)

            # Fade overlaps the start of this clip (fade-in or crossfade head)
            if trim_start < fade_end <= clip_end and fade_start <= clip_start:
                trim_start = max(trim_start, fade_end)

        if trim_end <= trim_start:
            warnings.append(f"Track '{track_name}': clip '{r.name}' fully consumed by fade(s), dropping")
            continue

        if trim_start != r.start_pos or trim_end != r.start_pos + r.length:
            offset_adjust = trim_start - r.start_pos
            trimmed_region = Region(
                index=r.index,
                name=r.name,
                audio_file=r.audio_file,
                start_pos=trim_start,
                sample_offset=r.sample_offset + offset_adjust,
                length=trim_end - trim_start,
            )
            result.append(TrackPlacement(p.track_name, p.track_index, trimmed_region))
        else:
            result.append(p)

    result.extend(fades)
    result.sort(key=lambda p: p.region.start_pos)
    return result


def _resolve_comp_overlaps(placements: list, track_name: str, warnings: list) -> list:
    """
    Build a non-overlapping timeline from possibly-overlapping comp clips.
    Shorter clips take priority (they represent specific edits); longer clips
    fill only unclaimed portions of their time range.
    """
    by_priority = sorted(placements, key=lambda p: (p.region.length, p.region.start_pos))
    claimed: list[tuple[int, int]] = []
    result: list = []

    for p in by_priority:
        r = p.region
        seg_start = r.start_pos
        seg_end = r.start_pos + r.length

        overlap_claimed = [(s, e) for s, e in claimed if s < seg_end and e > seg_start]

        if not overlap_claimed:
            result.append(p)
            claimed.append((seg_start, seg_end))
            continue

        # Find unclaimed sub-intervals within [seg_start, seg_end]
        events = sorted(set(
            [seg_start, seg_end] +
            [max(seg_start, s) for s, e in overlap_claimed] +
            [min(seg_end, e) for s, e in overlap_claimed]
        ))

        added_any = False
        for i in range(len(events) - 1):
            gap_start, gap_end = events[i], events[i + 1]
            if gap_end <= seg_start or gap_start >= seg_end:
                continue
            if any(s <= gap_start and e >= gap_end for s, e in overlap_claimed):
                continue  # fully claimed
            gap_offset = r.sample_offset + (gap_start - r.start_pos)
            gap_region = Region(r.index, r.name, r.audio_file, gap_start, gap_offset, gap_end - gap_start)
            result.append(TrackPlacement(p.track_name, p.track_index, gap_region))
            claimed.append((gap_start, gap_end))
            added_any = True

        if not added_any:
            warnings.append(f"Track '{track_name}': clip '{r.name}' fully covered by higher-priority clips, dropping")

    return sorted(result, key=lambda p: p.region.start_pos)


def _register_audio_defs(f):
    """Register MonoAudioGain operation/parameter defs if not already present."""
    try:
        f.dictionary.lookup_operationdef('MonoAudioGain')
    except Exception:
        op_def = f.create.from_name('OperationDef', _MONO_AUDIO_GAIN_AUID, 'MonoAudioGain', 'Gain Adjustment - Mono')
        op_def.media_kind = 'Sound'
        op_def['IsTimeWarp'].value = False
        op_def['NumberInputs'].value = 1
        f.dictionary.register_def(op_def)
        param_def = f.create.from_name('ParameterDef', _LEVEL_PARAM_AUID, 'LEVEL', 'Level/Gain', 'Rational')
        f.dictionary.register_def(param_def)


def _wrap_gain(f, seg, gain_num: int, gain_den: int = 1):
    """Wrap a segment in a MonoAudioGain OperationGroup."""
    og = f.create.OperationGroup('MonoAudioGain', media_kind='sound', length=seg.length)
    param = f.create.ConstantValue('LEVEL', AAFRational(gain_num, gain_den))
    og['Parameters'].append(param)
    og.segments.append(seg)
    return og



def _network_locator(f, path: str):
    n = f.create.NetworkLocator()
    n['URLString'].value = pathlib.Path(path).as_uri()
    return n


def write(session: SessionData, output_path: str, warnings: list | None = None) -> None:
    if warnings is None:
        warnings = []

    edit_rate = AAFRational(session.sample_rate, 1)

    with aaf2.open(output_path, 'w') as f:
        _register_audio_defs(f)
        master_mobs: dict[int, aaf2.mobs.MasterMob] = {}

        for af in session.audio_files:
            try:
                mm = _create_mob_chain(f, af, edit_rate, session.sample_rate, warnings)
                master_mobs[af.index] = mm
            except Exception as e:
                warnings.append(f"Skipping audio file '{af.filename}': {e}")

        comp = f.create.CompositionMob()
        comp.name = os.path.splitext(os.path.basename(output_path))[0]
        comp['UsageCode'].value = 'Usage_TopLevel'
        f.content.mobs.append(comp)

        # Group placements by track index
        tracks: dict[int, tuple[str, list[TrackPlacement]]] = {}
        for p in session.placements:
            if p.track_index not in tracks:
                tracks[p.track_index] = (p.track_name, [])
            tracks[p.track_index][1].append(p)

        for track_index in sorted(tracks.keys()):
            track_name, placements = tracks[track_index]
            placements = _resolve_comp_overlaps(placements, track_name, warnings)
            placements = _trim_for_fades(placements, track_name, warnings)
            placements.sort(key=lambda p: p.region.start_pos)

            slot = comp.create_sound_slot(edit_rate=edit_rate)
            slot.name = track_name
            # Build sequence independently so we can wrap it in pan/gain without
            # triggering pyaaf2's "Object already attached" guard (which fires when
            # appending a slot-owned sequence to an OperationGroup).
            seq = f.create.Sequence(media_kind='sound')

            cursor = 0

            for placement in placements:
                r = placement.region
                gap = r.start_pos - cursor

                if gap > 0:
                    filler = f.create.Filler(media_kind='sound', length=gap)
                    seq.components.append(filler)
                elif gap < 0:
                    warnings.append(
                        f"Track '{track_name}': overlapping clip '{r.name}' at sample "
                        f"{r.start_pos} (cursor={cursor}), skipping"
                    )
                    continue

                clip_length = r.length if r.length > 0 else r.audio_file.length
                if clip_length <= 0:
                    warnings.append(
                        f"Track '{track_name}': clip '{r.name}' has unknown length, skipping"
                    )
                    continue

                master_mob = master_mobs.get(r.audio_file.index)
                if master_mob is None:
                    filler = f.create.Filler(media_kind='sound', length=clip_length)
                    seq.components.append(filler)
                    warnings.append(
                        f"Track '{track_name}': missing mob for '{r.audio_file.filename}', inserting filler"
                    )
                else:
                    clip = master_mob.create_source_clip(
                        slot_id=1,
                        start=r.sample_offset,
                        length=clip_length,
                    )
                    seq.components.append(clip)

                cursor = r.start_pos + clip_length

            seq.length = cursor
            segment = seq

            vol_db, _pan = session.track_automation.get(track_name, (0.0, 0.0))

            # Gain: mute overrides fader volume. Logic Pro AAF import supports volume automation.
            if track_name in session.muted_tracks and cursor > 0:
                segment = _wrap_gain(f, segment, 0, 1)
            elif vol_db != 0.0 and cursor > 0:
                gain = 10.0 ** (vol_db / 20.0)
                frac = Fraction(gain).limit_denominator(100000)
                segment = _wrap_gain(f, segment, frac.numerator, frac.denominator)

            slot.segment = segment


def _create_mob_chain(f, af: AudioFile, edit_rate, sample_rate: int, warnings: list):
    is_aiff = af.filename.lower().endswith(('.aif', '.aiff'))
    file_exists = af.resolved_path is not None and os.path.exists(af.resolved_path)

    channels, bit_depth = 1, 24
    if file_exists:
        channels, bit_depth = _get_audio_channels_and_depth(af.resolved_path)

    # --- Tape mob (null origin) ---
    tape_mob = f.create.SourceMob()
    tape_mob.name = af.filename + ' <TAPE>'
    tape_slot = tape_mob.create_sound_slot(edit_rate=edit_rate)
    length = af.length if af.length > 0 else 1
    tape_clip = f.create.SourceClip(media_kind='sound', length=length)
    tape_slot.segment.components.append(tape_clip)
    tape_mob.descriptor = f.create.ImportDescriptor()
    f.content.mobs.append(tape_mob)

    # --- Source mob (the actual file) ---
    source_mob = f.create.SourceMob()
    source_mob.name = af.filename

    if is_aiff:
        desc = f.create.AIFCDescriptor()
        if file_exists:
            summary = aaf2.ama.get_aifc_fmt(af.resolved_path)
        else:
            summary = None
        if summary is None:
            summary = _make_wave_summary(channels, sample_rate, bit_depth)
            if not file_exists:
                warnings.append(f"'{af.filename}' not found on disk — using synthetic header")
        desc['SampleRate'].value = edit_rate
        desc['Summary'].value = summary
        desc['Length'].value = length
        desc['ContainerFormat'].value = f.dictionary.lookup_containerdef('AAF')
    else:
        desc = f.create.WAVEDescriptor()
        if file_exists:
            summary = aaf2.ama.get_wave_fmt(af.resolved_path)
        else:
            summary = None
        if summary is None:
            summary = _make_wave_summary(channels, sample_rate, bit_depth)
            if not file_exists:
                warnings.append(f"'{af.filename}' not found on disk — using synthetic WAVE header")
        desc['SampleRate'].value = edit_rate
        desc['Summary'].value = summary
        desc['Length'].value = length
        desc['ContainerFormat'].value = f.dictionary.lookup_containerdef('AAF')

    if file_exists:
        desc['Locator'].append(_network_locator(f, af.resolved_path))
    else:
        # Use a relative locator as fallback
        n = f.create.NetworkLocator()
        n['URLString'].value = f'file:///{af.filename}'
        desc['Locator'].append(n)

    source_mob.descriptor = desc

    # Source mob slot references tape mob
    src_clip = tape_mob.create_source_clip(slot_id=1, start=0, length=length)
    src_slot = source_mob.create_sound_slot(edit_rate=edit_rate)
    src_slot.segment.components.append(src_clip)
    f.content.mobs.append(source_mob)

    # --- Master mob ---
    master_mob = f.create.MasterMob()
    master_mob.name = af.filename

    mm_clip = source_mob.create_source_clip(slot_id=1, start=0, length=length)
    mm_slot = master_mob.create_sound_slot(edit_rate=edit_rate)
    mm_slot.segment.components.append(mm_clip)
    f.content.mobs.append(master_mob)

    return master_mob
