from __future__ import division

import os
import subprocess
from tempfile import TemporaryFile, NamedTemporaryFile
import wave
import audioop
import sys

try:
    from StringIO import StringIO
except:
    from io import StringIO, BytesIO

from .utils import (
    _fd_or_path_or_tempfile,
    db_to_float,
    ratio_to_db,
    get_encoder_name,
)
from .exceptions import (
    TooManyMissingFrames,
    InvalidDuration,
    InvalidID3TagVersion,
    InvalidTag,
)

if sys.version_info >= (3, 0):
    basestring = str
    xrange = range
    StringIO = BytesIO

AUDIO_FILE_EXT_ALIASES = {
    "m4a": "mp4"
}


class AudioSegment(object):

    """
    Note: AudioSegment objects are immutable

    slicable using milliseconds. for example: Get the first second of an mp3...

    a = AudioSegment.from_mp3(mp3file)
    first_second = a[:1000]
    """
    converter = get_encoder_name()  # either ffmpeg or avconv

    def __init__(self, data=None, *args, **kwargs):
        if kwargs.get('metadata', False):
            # internal use only
            self._data = data
            for attr, val in kwargs.pop('metadata').items():
                setattr(self, attr, val)
        else:
            # normal construction
            data = data if isinstance(data, basestring) else data.read()
            raw = wave.open(StringIO(data), 'rb')

            raw.rewind()
            self.channels = raw.getnchannels()
            self.sample_width = raw.getsampwidth()
            self.frame_rate = raw.getframerate()
            self.frame_width = self.channels * self.sample_width

            raw.rewind()
            self._data = raw.readframes(float('inf'))

        super(AudioSegment, self).__init__(*args, **kwargs)

    def __len__(self):
        """
        returns the length of this audio segment in milliseconds
        """
        return round(1000 * (self.frame_count() / self.frame_rate))

    def __eq__(self, other):
        try:
            return self._data == other._data
        except:
            return False

    def __ne__(self, other):
        return not (self == other)

    def __iter__(self):
        return (self[i] for i in xrange(len(self)))

    def __getitem__(self, millisecond):
        if isinstance(millisecond, slice):
            start = millisecond.start if millisecond.start is not None else 0
            end = millisecond.stop if millisecond.stop is not None \
                else len(self)

            start = min(start, len(self))
            end = min(end, len(self))
        else:
            start = millisecond
            end = millisecond + 1

        start = self._parse_position(start) * self.frame_width
        end = self._parse_position(end) * self.frame_width
        data = self._data[start:end]

        # ensure the output is as long as the requester is expecting
        expected_length = end - start
        missing_frames = (expected_length - len(data)) // self.frame_width
        if missing_frames:
            if missing_frames > self.frame_count(ms=2):
                raise TooManyMissingFrames("You should never be filling in "
                                           "   more than 2 ms with silence here, missing frames: %s" %
                                           missing_frames)
            silence = audioop.mul(data[:self.frame_width],
                                  self.sample_width, 0)
            data += (silence * missing_frames)

        return self._spawn(data)

    def get_sample_slice(self, start_sample=None, end_sample=None):
        """
        Get a section of the audio segment by sample index. NOTE: Negative
        indicies do you address samples backword from the end of the audio
        segment like a python list. This is intentional.
        """
        max_val = self.frame_count()

        def bounded(val, default):
            if val is None:
                return default
            if val < 0:
                return 0
            if val > max_val:
                return max_val
            return val

        start_i = bounded(start_sample, 0) * self.frame_width
        end_i = bounded(end_sample, max_val) * self.frame_width

        data = self._data[start_i:end_i]
        return self._spawn(data)

    def __add__(self, arg):
        if isinstance(arg, AudioSegment):
            return self.append(arg, crossfade=0)
        else:
            return self.apply_gain(arg)

    def __sub__(self, arg):
        if isinstance(arg, AudioSegment):
            raise TypeError("AudioSegment objects can't be subtracted from "
                            "each other")
        else:
            return self.apply_gain(-arg)

    def __mul__(self, arg):
        """
        If the argument is an AudioSegment, overlay the multiplied audio
        segment.


        If it's a number, just use the string multiply operation to repeat the
        audio so the following would return an AudioSegment that contains the
        audio of audio_seg eight times

        audio_seg * 8
        """
        if isinstance(arg, AudioSegment):
            return self.overlay(arg, position=0, loop=True)
        else:
            return self._spawn(data=self._data * arg)

    def _spawn(self, data, overrides={}):
        """
        Creates a new audio segment using the meta data from the current one
        and the data passed in. Should be used whenever an AudioSegment is
        being returned by an operation that alters the current one, since
        AudioSegment objects are immutable.
        """
        # accept lists of data chunks
        if isinstance(data, list):
            data = b''.join(data)

        # accept file-like objects
        if hasattr(data, 'read'):
            if hasattr(data, 'seek'):
                data.seek(0)
            data = data.read()

        metadata = {
            'sample_width': self.sample_width,
            'frame_rate': self.frame_rate,
            'frame_width': self.frame_width,
            'channels': self.channels
        }
        metadata.update(overrides)
        return AudioSegment(data=data, metadata=metadata)

    @classmethod
    def _sync(cls, seg1, seg2):
        s1_len, s2_len = len(seg1), len(seg2)

        channels = max(seg1.channels, seg2.channels)
        seg1 = seg1.set_channels(channels)
        seg2 = seg2.set_channels(channels)

        frame_rate = max(seg1.frame_rate, seg2.frame_rate)
        seg1 = seg1.set_frame_rate(frame_rate)
        seg2 = seg2.set_frame_rate(frame_rate)

        assert(len(seg1) == s1_len)
        assert(len(seg2) == s2_len)

        return seg1, seg2

    def _parse_position(self, val):
        if val < 0:
            val = len(self) - abs(val)
        val = self.frame_count(ms=len(self)) if val == float("inf") else \
            self.frame_count(ms=val)
        return int(val)

    @classmethod
    def empty(cls):
        return cls(b'', metadata={"channels": 1, "sample_width": 1, "frame_rate": 1, "frame_width": 1})

    @classmethod
    def from_file(cls, file, format=None):
        file = _fd_or_path_or_tempfile(file, 'rb', tempfile=False)

        if format:
            format = AUDIO_FILE_EXT_ALIASES.get(format, format)

        if format == 'wav':
            return cls.from_wav(file)

        with NamedTemporaryFile('w+b') as input_file:
            # input_file = NamedTemporaryFile(mode='wb', delete=False)
            input_file.write(file.read())
            input_file.flush()

            # output = NamedTemporaryFile(mode="rb", delete=False)
            with NamedTemporaryFile('w+b') as output:

                convertion_command = [cls.converter,
                                      '-y',  # always overwrite existing files
                                      ]

                # If format is not defined
                # ffmpeg/avconv will detect it automatically
                if format:
                    convertion_command += ["-f", format]

                convertion_command += [
                    "-i", input_file.name,  # input_file options (filename last)
                    "-vn",  # Drop any video streams if there are any
                    "-f", "wav",  # output options (filename last)
                    output.name
                ]

                subprocess.call(convertion_command, stderr=open(os.devnull))

                obj = cls.from_wav(output)

        return obj

    @classmethod
    def from_mp3(cls, file):
        return cls.from_file(file, 'mp3')

    @classmethod
    def from_flv(cls, file):
        return cls.from_file(file, 'flv')

    @classmethod
    def from_ogg(cls, file):
        return cls.from_file(file, 'ogg')

    @classmethod
    def from_wav(cls, file):
        file = _fd_or_path_or_tempfile(file, 'rb', tempfile=False)
        file.seek(0)
        return cls(data=file)

    def export(self, out_f=None, format='mp3', codec=None, bitrate=None, parameters=None, tags=None, id3v2_version='4'):
        """
        Export an AudioSegment to a file with given options

        out_f (string):
            Path to destination audio file

        format (string)
            Format for destination audio file. ('mp3', 'wav', 'ogg' or other ffmpeg/avconv supported files)

        codec (string)
            Codec used to encoding for the destination.

        bitrate (string)
            Bitrate used when encoding destination file. (128, 256, 312k...)

        parameters (string)
            Aditional ffmpeg/avconv parameters

        tags (dict)
            Set metadata information to destination files usually used as tags. ({title='Song Title', artist='Song Artist'})

        id3v2_version (string)
            Set ID3v2 version for tags. (default: '4')
        """
        id3v2_allowed_versions = ['3', '4']

        out_f = _fd_or_path_or_tempfile(out_f, 'wb+')
        out_f.seek(0)

        with NamedTemporaryFile('w+b') as data:
            if format == "wav":
                data = out_f

            wave_data = wave.open(data, 'wb')
            wave_data.setnchannels(self.channels)
            wave_data.setsampwidth(self.sample_width)
            wave_data.setframerate(self.frame_rate)
            # For some reason packing the wave header struct with a float in python 2
            # doesn't throw an exception
            wave_data.setnframes(int(self.frame_count()))
            wave_data.writeframesraw(self._data)
            wave_data.close()

            # for wav files, we're done (wav data is written directly to out_f)
            if format == 'wav':
                return out_f

            # output = NamedTemporaryFile(mode="w+b", delete=False)
            with NamedTemporaryFile('w+b') as output:

                # build converter command to export
                convertion_command = [self.converter,
                                      '-y',  # always overwrite existing files
                                      "-f", "wav", "-i", data.name,  # input options (filename last)
                                      ]

                if format == "ogg" and codec is None:
                    convertion_command.extend(["-acodec", "libvorbis"])

                if codec is not None:
                    # force audio encoder
                    convertion_command.extend(["-acodec", codec])

                if bitrate is not None:
                    convertion_command.extend(["-b:a", bitrate])

                if parameters is not None:
                    # extend arguments with arbitrary set
                    convertion_command.extend(parameters)

                if tags is not None:
                    if not isinstance(tags, dict):
                        raise InvalidTag("Tags must be a dictionary.")
                    else:
                        # Extend converter command with tags
                        for key, value in tags.items():
                            convertion_command.extend(['-metadata', '{0}={1}'.format(key, value)])

                        if format == 'mp3':
                            # set id3v2 tag version for mp3 files
                            if id3v2_version not in id3v2_allowed_versions:
                                raise InvalidID3TagVersion(
                                    "id3v2_version not allowed, allowed versions: %s" % id3v2_allowed_versions)
                            convertion_command.extend([
                                "-id3v2_version",  id3v2_version
                            ])

                convertion_command.extend([
                    "-f", format, output.name,  # output options (filename last)
                ])

                # read stdin / write stdout
                subprocess.call(convertion_command,
                                # make converter shut up
                                stderr=open(os.devnull)
                                )

                output.seek(0)
                out_f.write(output.read())
                out_f.seek(0)

        return out_f

    def get_frame(self, index):
        frame_start = index * self.frame_width
        frame_end = frame_start + self.frame_width
        return self._data[frame_start:frame_end]

    def frame_count(self, ms=None):
        """
        returns the number of frames for the given number of milliseconds, or
            if not specified, the number of frames in the whole AudioSegment
        """
        if ms is not None:
            return ms * (self.frame_rate / 1000.0)
        else:
            return float(len(self._data) // self.frame_width)

    def set_frame_rate(self, frame_rate):
        if frame_rate == self.frame_rate:
            return self

        if self._data:
            converted, _ = audioop.ratecv(self._data, self.sample_width,
                                          self.channels, self.frame_rate,
                                          frame_rate, None)
        else:
            converted = self._data

        return self._spawn(data=converted,
                           overrides={'frame_rate': frame_rate})

    def set_channels(self, channels):
        if channels == self.channels:
            return self

        if channels == 2 and self.channels == 1:
            fn = 'tostereo'
            frame_width = self.frame_width * 2
        elif channels == 1 and self.channels == 2:
            fn = 'tomono'
            frame_width = self.frame_width // 2

        fn = getattr(audioop, fn)
        converted = fn(self._data, self.sample_width, 1, 1)

        return self._spawn(data=converted, overrides={'channels': channels,
                                                      'frame_width': frame_width})

    @property
    def rms(self):
        return audioop.rms(self._data, self.sample_width)

    @property
    def dBFS(self):
        rms = self.rms
        if not rms:
            return -float("infinity")
        return ratio_to_db(self.rms / self.max_possible_amplitude)

    @property
    def max(self):
        return audioop.max(self._data, self.sample_width)

    @property
    def max_possible_amplitude(self):
        bits = self.sample_width * 8
        max_possible_val = (2 ** bits)

        # since half is above 0 and half is below the max amplitude is divided
        return max_possible_val / 2

    @property
    def duration_seconds(self):
        return self.frame_rate and self.frame_count() / self.frame_rate or 0.0

    def apply_gain(self, volume_change):
        return self._spawn(data=audioop.mul(self._data, self.sample_width,
                                            db_to_float(float(volume_change))))

    def overlay(self, seg, position=0, loop=False):
        output = TemporaryFile()

        seg1, seg2 = AudioSegment._sync(self, seg)
        sample_width = seg1.sample_width
        spawn = seg1._spawn

        output.write(seg1[:position]._data)

        # drop down to the raw data
        seg1 = seg1[position:]._data
        seg2 = seg2._data
        pos = 0
        seg1_len = len(seg1)
        seg2_len = len(seg2)
        while True:
            remaining = max(0, seg1_len - pos)
            if seg2_len >= remaining:
                seg2 = seg2[:remaining]
                seg2_len = remaining
                loop = False

            output.write(audioop.add(seg1[pos:pos + seg2_len], seg2,
                                     sample_width))
            pos += seg2_len

            if not loop:
                break

        output.write(seg1[pos:])

        return spawn(data=output)

    def append(self, seg, crossfade=100):
        output = TemporaryFile()

        seg1, seg2 = AudioSegment._sync(self, seg)

        if not crossfade:
            return seg1._spawn(seg1._data + seg2._data)

        xf = seg1[-crossfade:].fade(to_gain=-120, start=0, end=float('inf'))
        xf *= seg2[:crossfade].fade(from_gain=-120, start=0, end=float('inf'))

        output.write(seg1[:-crossfade]._data)
        output.write(xf._data)
        output.write(seg2[crossfade:]._data)

        output.seek(0)
        return seg1._spawn(data=output)

    def fade(self, to_gain=0, from_gain=0, start=None, end=None,
             duration=None):
        """
        Fade the volume of this audio segment.

        to_gain (float):
            resulting volume_change in db

        start (int):
            default = beginning of the segment
            when in this segment to start fading in milliseconds

        end (int):
            default = end of the segment
            when in this segment to start fading in milliseconds

        duration (int):
            default = until the end of the audio segment
            the duration of the fade
        """
        if None not in [duration, end, start]:
            raise TypeError('Only two of the three arguments, "start", '
                            '"end", and "duration" may be specified')

        # no fade == the same audio
        if to_gain == 0 and from_gain == 0:
            return self

        frames = self.frame_count()

        start = min(len(self), start) if start is not None else None
        end = min(len(self), end) if end is not None else None

        if start is not None and start < 0:
            start += len(self)
        if end is not None and end < 0:
            end += len(self)

        if duration is not None and duration < 0:
            raise InvalidDuration("duration must be a positive integer")

        if duration:
            if start is not None:
                end = start + duration
            elif end is not None:
                start = end - duration
        else:
            duration = end - start

        from_power = db_to_float(from_gain)

        output = []

        # original data - up until the crossfade portion, as is
        before_fade = self[:start]._data
        if from_gain != 0:
            before_fade = audioop.mul(before_fade, self.sample_width,
                                      from_power)
        output.append(before_fade)

        gain_delta = db_to_float(to_gain) - from_power

        # fades longer than 100ms can use coarse fading (one gain step per ms),
        # shorter fades will have audible clicks so they use precise fading (one
        # gain step per sample)
        if duration > 100:
            scale_step = gain_delta / duration

            for i in range(duration):
                volume_change = from_power + (scale_step * i)
                chunk = self[start + i]
                chunk = audioop.mul(chunk._data, self.sample_width, volume_change)

                output.append(chunk)
        else:
            start_frame = self.frame_count(ms=start)
            end_frame = self.frame_count(ms=end)
            fade_frames = end_frame - start_frame
            scale_step = gain_delta / fade_frames

            for i in range(int(fade_frames)):
                volume_change = from_power + (scale_step * i)
                sample = self.get_frame(int(start_frame + i))
                sample = audioop.mul(sample, self.sample_width, volume_change)

                output.append(sample)

        # original data after the crossfade portion, at the new volume
        after_fade = self[end:]._data
        if to_gain != 0:
            after_fade = audioop.mul(after_fade, self.sample_width,
                                     db_to_float(to_gain))
        output.append(after_fade)

        return self._spawn(data=output)

    def fade_out(self, duration):
        return self.fade(to_gain=-120, duration=duration, end=float('inf'))

    def fade_in(self, duration):
        return self.fade(from_gain=-120, duration=duration, start=0)

    def reverse(self):
        return self._spawn(
            data=audioop.reverse(self._data, self.sample_width)
        )


from . import effects
