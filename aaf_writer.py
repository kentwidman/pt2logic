"""
Generates an AAF file from parsed Pro Tools session data.
The AAF can be imported into Logic Pro X via File → Import → AAF.
"""

import os
import struct
import pathlib
import sys

import aaf2
import aaf2.mobs
import aaf2.components
import aaf2.ama
from aaf2.rational import AAFRational

from ptx_parser import SessionData, AudioFile, TrackPlacement


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
            f.seek(20)
            data = f.read(14)
        if len(data) < 14:
            return 1, 24
        channels = struct.unpack_from('<H', data, 2)[0]
        bit_depth = struct.unpack_from('<H', data, 12)[0]
        return max(1, channels), max(8, bit_depth)
    except OSError:
        return 1, 24


def _network_locator(f, path: str):
    n = f.create.NetworkLocator()
    n['URLString'].value = pathlib.Path(path).as_uri()
    return n


def write(session: SessionData, output_path: str, warnings: list | None = None) -> None:
    if warnings is None:
        warnings = []

    edit_rate = AAFRational(session.sample_rate, 1)

    with aaf2.open(output_path, 'w') as f:
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
            placements.sort(key=lambda p: p.region.start_pos)

            slot = comp.create_sound_slot(edit_rate=edit_rate)
            slot.name = track_name
            seq = slot.segment  # Sequence object

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

                master_mob = master_mobs.get(r.audio_file.index)
                if master_mob is None:
                    filler = f.create.Filler(media_kind='sound', length=r.length)
                    seq.components.append(filler)
                    warnings.append(
                        f"Track '{track_name}': missing mob for '{r.audio_file.filename}', inserting filler"
                    )
                else:
                    clip = master_mob.create_source_clip(
                        slot_id=1,
                        start=r.sample_offset,
                        length=r.length,
                    )
                    seq.components.append(clip)

                cursor = r.start_pos + r.length

            # Set sequence and slot length to total duration
            seq.length = cursor


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
