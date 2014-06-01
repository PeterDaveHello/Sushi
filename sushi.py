import logging
from common import SushiError, get_extension, format_time
from demux import Timecodes, Demuxer
from keyframes import parse_keyframes
from subs import AssScript, SrtScript
from wav import WavStream
import sys
from itertools import takewhile
import numpy as np
import argparse
import chapters
import os
from time import time
import bisect

ALLOWED_ERROR = 0.01
MAX_GROUP_STD = 0.025
MAX_REASONABLE_DIFF = 0.5


def abs_diff(a, b):
    return abs(a - b)


# todo: implement this as a running median
def smooth_events(events, window_size):
    if window_size % 2 != 1:
        raise SushiError('Median window size should be odd')
    half_window = window_size // 2
    for x in xrange(half_window, len(events) - half_window):
        start = max(0, x - half_window)
        end = x + half_window + 1
        med = np.median([e.shift for e in events[start:end]])
        events[x].set_shift(med, events[x].diff)


def detect_groups(events, min_group_size):
    smooth_events([x for x in events if not x.linked], 7)  # smoothing events for better group detection

    last_shift = events[0].shift
    current_group = []
    groups = []
    for e in events:
        if abs_diff(e.shift, last_shift) > ALLOWED_ERROR:
            groups.append(current_group)
            current_group = [e]
            last_shift = e.shift
        else:
            current_group.append(e)
    groups.append(current_group)

    if not any(g for g in groups if len(g) >= min_group_size):
        return groups  # not a single large group to merge into

    large = []
    groups = iter(groups)
    a = next(groups, None)
    while a is not None:
        if len(a) >= min_group_size:
            large.append(a)
            a = next(groups, None)
        else:
            small = []
            while a and len(a) < min_group_size:
                small.extend(a)
                a = next(groups, None)

            if not large:
                a = small + a  # no large groups before, extend the next one
            elif not a:
                large[-1].extend(small)  # next group doesn't exist, extend the previous one
            elif abs_diff(a[0].shift, small[-1].shift) < abs_diff(large[-1][-1].shift, small[0].shift):
                a = small + a  # next group has closer diff then the next one
            else:
                large[-1].extend(small)

    return large


def groups_from_chapters(events, times, min_auto_group_size):
    times = list(times)  # copy
    logging.debug(u'Chapter start points: {0}'.format([format_time(t) for t in times]))
    times.append(36000000000)  # very large event at the end
    groups = [[]]
    current_chapter = 0

    for event in events:
        if event.start > times[current_chapter + 1]:
            groups.append([])
            current_chapter += 1

        groups[-1].append(event)

    if not groups[-1]:
        del groups[-1]

    correct_groups = []
    broken_found = False
    for g in groups:
        std = np.std([e.shift for e in g])
        if std > MAX_GROUP_STD:
            logging.warn(u'Shift is not consistent between {0} and {1}, most likely chapters are wrong (std: {2}). '
                         u'Switching to automatic grouping.'.format(format_time(g[0].start), format_time(g[-1].end), std))
            correct_groups.extend(detect_groups(g, min_auto_group_size))
            broken_found = True
        else:
            correct_groups.append(g)

    if broken_found:
        correct_groups = sorted(correct_groups, key=lambda g: g[0].start)

        i = 0
        while i < len(correct_groups) - 1:
            if abs_diff(correct_groups[i][-1].shift, correct_groups[i + 1][0].shift) < ALLOWED_ERROR \
                    and np.std([e.shift for e in correct_groups[i] + correct_groups[i + 1]]) < MAX_GROUP_STD:
                correct_groups[i].extend(correct_groups[i + 1])
                del correct_groups[i + 1]
            else:
                i += 1

    return correct_groups


def calculate_shifts(src_stream, dst_stream, events, chapter_times, window, fast_skip, max_ts_duration, max_ts_distance):
    small_window = 2
    last_shift = 0

    for idx, event in enumerate(events):
        if event.start > src_stream.duration_seconds:
            logging.info('Event time outside of audio range, ignoring: %s' % unicode(event))
            event.mark_broken()
        elif event.end == event.start:
            logging.debug('{0}: skipped because zero duration'.format(format_time(event.start)))
            if idx == 0:
                event.mark_broken()
            else:
                event.link_event(events[idx-1])
        elif fast_skip:
            # assuming scripts are sorted by start time so we don't search the entire collection
            same_start = lambda x: event.start == x.start
            processed = next((x for x in takewhile(same_start, reversed(events[:idx])) if not x.linked and x.end == event.end), None)
            if processed:
                # logging.debug('{0}-{1}: skipped because identical to already processed (typesetting?)'
                #               .format(format_time(event.start), format_time(event.end)))
                event.link_event(processed)

    events = (e for e in events if not e.linked and not e.broken)

    search_groups = []
    chapter_times = iter(chapter_times[1:] + [100000000])
    next_chapter = next(chapter_times)
    event = next(events, None)
    while event:
        while event.end > next_chapter:
            next_chapter = next(chapter_times)

        if event.duration > max_ts_duration:
            search_groups.append([event])
            event = next(events, None)
        else:
            group = [event]
            event = next(events, None)
            while event and event.duration < max_ts_duration and abs(event.start - group[-1].end) < max_ts_distance\
                    and event.end <= next_chapter:
                group.append(event)
                event = next(events, None)

            search_groups.append(group)

    for search_group in search_groups:
        tv_audio = src_stream.get_substream(search_group[0].start, search_group[-1].end)

        original_time = search_group[0].start
        start_point = original_time + last_shift

        # searching with smaller window
        diff = new_time = None
        if small_window < window:
            diff, new_time = dst_stream.find_substream(tv_audio,
                                                       start_time=start_point - small_window,
                                                       end_time=start_point + small_window)

        # checking if times are close enough to last shift - no point in re-searching with full window if it's in the same group
        if not new_time or abs_diff(new_time - original_time, last_shift) > ALLOWED_ERROR:
            diff, new_time = dst_stream.find_substream(tv_audio,
                                                       start_time=start_point - window,
                                                       end_time=start_point + window)

        last_shift = time_offset = new_time - original_time

        for e in search_group:
            e.set_shift(time_offset, diff)
            logging.debug('{0}-{1}: shift: {2:0.12f}, diff: {3:0.12f}'
                          .format(format_time(e.start), format_time(e.end), time_offset, diff))


def clip_obviously_wrong(events):
    for idx, event in enumerate(events):
        if event.diff < MAX_REASONABLE_DIFF:  # somewhat random
            continue
        next_sane = next(x for x in events[idx + 1:] if x.diff < MAX_REASONABLE_DIFF)

        # select the one with closest shift
        prev_sane = events[idx - 1]  # first is never broken because it's fixed in borders routine
        if abs_diff(prev_sane.shift, event.shift) < abs_diff(next_sane.shift, event.shift):
            event.link_event(prev_sane)
        else:
            event.link_event(next_sane)


def fix_near_borders(events):
    def fix_border(event_list):
        broken = list(takewhile(lambda x: x.diff > MAX_REASONABLE_DIFF, event_list))
        if broken:
            sane = event_list[len(broken)]
            for x in broken:
                x.link_event(sane)

    fix_border(events)
    fix_border(list(reversed(events)))


def find_keyframes_nearby(events, keyframes):
    def find_closest_kf(timestamp):
        idx = bisect.bisect_left(keyframes, timestamp)
        if idx == 0:
            return keyframes[0]
        if idx == len(keyframes):
            return keyframes[-1]
        before = keyframes[idx - 1]
        after = keyframes[idx]
        return after if after - timestamp < timestamp - before else before

    for event in events:
        before = find_closest_kf(event.start + event.shift)
        after = find_closest_kf(event.end + event.shift)
        event.set_keyframes(before, after)


def snap_to_keyframes(events, timecodes, max_kf_distance, max_kf_snapping):
    max_kf_distance = max(max_kf_distance, max_kf_snapping)
    distances = []
    frame_sizes = []
    for event in events:
        frame_sizes.append(timecodes.get_frame_size(event.start))
        for d in event.get_keyframes_distances():
            if d is not None and abs(d) < (max_kf_distance * frame_sizes[-1]):
                distances.append(d)

    mean_fs = np.mean(frame_sizes)
    if not distances:
        logging.info('No lines close enough to keyframes. Mean frame size: {0}'.format(mean_fs))
        return
    mean = np.mean(distances)
    snap = abs(mean) < (max_kf_snapping * mean_fs)
    logging.info('{0} to keyframes [distance: {1}, frame size: {2}]'
                  .format('Snapping' if snap else 'Not snapping', mean, mean_fs))

    if snap:
        for event in filter(lambda x: not x.linked, events):
            event.adjust_shift(mean)


def average_shifts(events):
    events = [e for e in events if not e.linked]
    shifts = [x.shift for x in events]
    weights = [1 - x.diff for x in events]
    avg, weights_sum = np.average(shifts, weights=weights, returned=True)
    new_diff = 1 - weights_sum / len(events)
    for e in events:
        e.set_shift(avg, new_diff)
    return avg


def apply_shifts(events):
    for e in events:
        e.apply_shift()


def check_file_exists(path, file_title):
    if path and not os.path.exists(path):
        raise SushiError("{0} file doesn't exist".format(file_title))


def run(args):
    format = "%(levelname)s: %(message)s"
    logging.basicConfig(level=logging.DEBUG, format=format)
    ignore_chapters = args.chapters_file is not None and args.chapters_file.lower() == 'none'

    # first part should do all possible validation and should NOT take significant amount of time
    check_file_exists(args.source, 'Source')
    check_file_exists(args.destination, 'Destination')
    check_file_exists(args.timecodes_file, 'Timecodes')
    check_file_exists(args.keyframes_file, 'Keyframes')
    check_file_exists(args.script_file, 'Script')

    if not ignore_chapters:
        check_file_exists(args.chapters_file, 'Chapters')

    if args.timecodes_file and args.dst_fps:
        raise SushiError('Both fps and timecodes file cannot be specified at the same time')

    src_demuxer = Demuxer(args.source)
    dst_demuxer = Demuxer(args.destination)

    if src_demuxer.is_wav and not args.script_file:
        raise SushiError("Script file isn't specified")

    # selecting source audio
    if src_demuxer.is_wav:
        src_audio_path = args.source
    else:
        src_audio_path = args.source + '.sushi.wav'
        src_demuxer.set_audio(stream_idx=args.src_audio_idx, output_path=src_audio_path, sample_rate=args.sample_rate)

    # selecting destination audio
    if dst_demuxer.is_wav:
        dst_audio_path = args.destination
    else:
        dst_audio_path = args.destination + '.sushi.wav'
        dst_demuxer.set_audio(stream_idx=args.dst_audio_idx, output_path=dst_audio_path, sample_rate=args.sample_rate)

    # selecting source subtitles
    if args.script_file:
        src_script_path = args.script_file
    else:
        stype = src_demuxer.get_subs_type(args.src_script_idx)
        src_script_path = args.source + '.sushi' + stype
        src_demuxer.set_script(stream_idx=args.src_script_idx, output_path=src_script_path)

    script_extension = get_extension(src_script_path)
    if script_extension not in ('.ass', '.src'):
        raise SushiError('Unknown script type')

    # selection destination subtitles
    if args.output_script:
        dst_script_path = args.output_script
        dst_script_extension = get_extension(args.output_script)
        if dst_script_extension != script_extension:
            raise SushiError("Source and destination script file types don't match ({0} vs {1})"
                             .format(script_extension, dst_script_extension))
    else:
        dst_script_path = args.destination + '.sushi' + script_extension

    # selecting chapters
    if args.grouping and not ignore_chapters:
        if args.chapters_file:
            if get_extension(args.chapters_file) == '.xml':
                chapter_times = chapters.get_xml_start_times(args.chapters_file)
            else:
                chapter_times = chapters.get_ogm_start_times(args.chapters_file)
        elif not src_demuxer.is_wav:
            chapter_times = src_demuxer.chapters
        else:
            chapter_times = []
    else:
        chapter_times = []

    # selecting keyframes and timecodes
    if args.keyframes_file:
        keyframes = parse_keyframes(args.keyframes_file)
        if not keyframes:
            raise SushiError('No keyframes found in the provided keyframes file')

        if args.timecodes_file:
            timecodes_file = args.timecodes_file
        elif args.dst_fps:
            timecodes_file = None
        elif dst_demuxer.has_video:
            timecodes_file = args.destination + '.sushi.timecodes.txt'
            dst_demuxer.set_timecodes(output_path=timecodes_file)
        else:
            raise SushiError('Fps, timecodes or video files must be provided if keyframes are used')
    else:
        keyframes = None

    # after this point nothing should fail so it's safe to start slow operations
    # like running the actual demuxing
    src_demuxer.demux()
    dst_demuxer.demux()

    try:
        if keyframes:
            timecodes = Timecodes.cfr(args.dst_fps) if args.dst_fps else Timecodes.from_file(timecodes_file)
            keytimes = [timecodes.get_frame_time(f) for f in keyframes]

        script = AssScript(src_script_path) if script_extension == '.ass' else SrtScript(src_script_path)
        script.sort_by_time()

        src_stream = WavStream(src_audio_path, sample_rate=args.sample_rate, sample_type=args.sample_type)
        dst_stream = WavStream(dst_audio_path, sample_rate=args.sample_rate, sample_type=args.sample_type)

        calculate_shifts(src_stream, dst_stream, script.events,
                         chapter_times=chapter_times,
                         window=args.window,
                         fast_skip=args.fast_skip,
                         max_ts_duration=args.max_ts_duration,
                         max_ts_distance=args.max_ts_distance)

        events = [x for x in script.events if not x.broken]

        fix_near_borders(events)
        clip_obviously_wrong(events)

        if args.grouping:
            if not ignore_chapters and chapter_times:
                groups = groups_from_chapters(events, chapter_times, args.min_group_size)
            else:
                groups = detect_groups(events, args.min_group_size)

            for g in groups:
                start_shift = g[0].shift
                end_shift = g[-1].shift
                avg_shift = average_shifts(g)
                logging.info(u'Group (start: {0}, end: {1}, lines: {2}), '
                             u'shifts (start: {3}, end: {4}, average: {5})'
                             .format(format_time(g[0].start), format_time(g[-1].end), len(g), start_shift, end_shift, avg_shift))
                if keyframes:
                    find_keyframes_nearby(g, keytimes)
                    snap_to_keyframes(g, timecodes, args.max_kf_distance, args.max_kf_snapping)
        elif keyframes:
            find_keyframes_nearby(events, keytimes)
            snap_to_keyframes(events, timecodes, args.max_kf_distance, args.max_kf_snapping)

        apply_shifts(events)

        script.save_to_file(dst_script_path)

    finally:
        if args.cleanup:
            src_demuxer.cleanup()
            dst_demuxer.cleanup()


def create_arg_parser():
    parser = argparse.ArgumentParser(description='Sushi - Automatic Subtitle Shifter')

    parser.add_argument('--window', default=10, type=int, metavar='<size>', dest='window',
                        help='Search window size')
    parser.add_argument('--no-grouping', action='store_false', dest='grouping',
                        help='Split events into groups before shifting')
    parser.add_argument('--min-group-size', default=1, type=int, dest='min_group_size',
                        help='Minimum size of automatic group')
    parser.add_argument('--max-kf-distance', default=3, type=float, metavar='<frames>', dest='max_kf_distance',
                        help='Maximum distance to keyframe for it to be considered in keyframe snapping [3]')
    parser.add_argument('--max-kf-snapping', default=0.75, type=float, metavar='<frames>', dest='max_kf_snapping',
                        help='Maximum keyframe snapping distance [0.75]')

     # 2.5 frames at 23.976
    parser.add_argument('--max-ts-duration', default=1001.0 / 24000.0 * 2.5, type=float, metavar='<seconds>', dest='max_ts_duration',
                        help='Maximum duration of a line to be considered typesetting')
    # 5 frames at 23.976
    parser.add_argument('--max-ts-distance', default=1001.0 / 24000.0 * 5, type=float, metavar='<seconds>', dest='max_ts_distance',
                        help='Maximum distance between two adjacent typesetting lines to be merged')

    # optimizations
    parser.add_argument('--no-fast-skip', action='store_false', dest='fast_skip',
                        help="Don't skip lines with identical timing")

    parser.add_argument('--sample-rate', default=12000, type=int, metavar='<rate>', dest='sample_rate',
                        help='Downsampled audio sample rate')
    parser.add_argument('--sample-type', default='uint8', choices=['float32', 'uint8'], dest='sample_type',
                        help='Downsampled audio representation type')

    parser.add_argument('--src-audio', default=None, type=int, metavar='<id>', dest='src_audio_idx',
                        help='Audio stream index of the source video')
    parser.add_argument('--src-script', default=None, type=int, metavar='<id>', dest='src_script_idx',
                        help='Script stream index of the source video')
    parser.add_argument('--dst-audio', default=None, type=int, metavar='<id>', dest='dst_audio_idx',
                        help='Audio stream index of the destination video')
    # files
    parser.add_argument('--no-cleanup', action='store_false', dest='cleanup',
                        help="Don't delete demuxed streams")
    parser.add_argument('--chapters', default=None, dest='chapters_file', metavar='<filename>',
                        help="XML or OGM chapters to use instead of any found in the source. 'none' to disable.")
    parser.add_argument('--script', default=None, dest='script_file', metavar='<filename>',
                        help='Subtitle file path to use instead of any found in the source')
    parser.add_argument('--keyframes', default=None, dest='keyframes_file', metavar='<filename>',
                        help='Keyframes file path to use.')
    parser.add_argument('--fps', default=None, type=float, dest='dst_fps', metavar='<fps>',
                        help='Fps of destination video. Must be provided if keyframes are used.')
    parser.add_argument('--timecodes', default=None, dest='timecodes_file', metavar='<filename>',
                        help='Timecodes file to use instead of making one from the source (when possible)')

    parser.add_argument('--src', required=True, dest="source", metavar='<filename>',
                        help='Source audio/video')
    parser.add_argument('--dst', required=True, dest="destination", metavar='<filename>',
                        help='Destination audio/video')
    parser.add_argument('-o', '--output', default=None, dest='output_script', metavar='<filename>',
                        help='Output script')

    return parser


def parse_args_and_run(cmd_keys):
    args = create_arg_parser().parse_args(cmd_keys)
    start_time = time()
    run(args)
    logging.info('Done in {0}s'.format(time() - start_time))


if __name__ == '__main__':
    try:
        parse_args_and_run(sys.argv[1:])
    except SushiError as e:
        logging.critical(e.message)