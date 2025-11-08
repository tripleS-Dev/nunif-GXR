"""HTTP streaming server for iw3 Web Streaming.

The original implementation exposed a multipart MJPEG stream so that the
frontend could update a ``<canvas>`` with sequential JPEG snapshots.  In order
to make browsers present native video playback controls (seek bar, remaining
time, etc.) we now expose an MP4 container stream that is produced on the fly
with PyAV/FFmpeg.  The stream is provided as fragmented MP4 (``movflags``
``empty_moov``) so that it can be consumed while it is being produced.
"""
import sys
import time
import threading
from string import Template
import io
from socketserver import ThreadingMixIn
from wsgiref.simple_server import make_server, WSGIServer
import random
import json
import base64
from collections import deque, defaultdict
from fractions import Fraction

import numpy as np
import av
import torch


STATUS_OK = "200 OK"


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True
    block_on_close = False


class _StreamingBuffer(io.BytesIO):
    """In-memory buffer that lets PyAV append data incrementally."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()

    def write(self, b):  # noqa: D401 - signature defined by io.RawIOBase
        with self._lock:
            self.seek(0, io.SEEK_END)
            super().write(b)
        return len(b)

    def read_new(self):
        """Return newly written bytes and clear the internal storage."""

        with self._lock:
            self.seek(0)
            data = super().read()
            self.truncate(0)
            self.seek(0)
        return data


class StreamingServer():
    def __init__(
            self, port, lock,
            frame_width, frame_height, fps,
            index_template,
            stream_uri="/stream.mp4", stream_content_type="video/mp4",
            auth=None, host=""):
        self.port = port
        self.host = host
        self.lock = lock
        self.op_lock = threading.Lock()
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.fps = fps
        self.delay = 1.0 / fps
        self.index_template = index_template
        self.stream_uri = stream_uri
        self.stream_content_type = stream_content_type
        self.fake_duration = 3600  # seconds, used to show a remaining time bar

        self.frame_data = None
        self.frame_data_raw = None

        self.server = None
        self.thread = None
        self.process_token = None
        self.shutdown_event = threading.Event()
        self.fps_counter = deque(maxlen=120)

        if auth is not None:
            user, password = auth
            self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        else:
            self.auth = None

    def _stop(self):
        self.shutdown_event.set()
        time.sleep(0.1)
        if self.server is not None:
            self.server.server_close()
            self.server.shutdown()
            self.server = None
        if self.thread is not None:
            if self.thread.ident is not None:
                self.thread.join()
            self.thread = None

        time.sleep(0.1)
        self.shutdown_event.clear()

    def _start(self):
        self.server = make_server(self.host, self.port, self.handle, ThreadingWSGIServer)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.process_token = "%016x" % random.getrandbits(64)

    def stop(self):
        with self.op_lock:
            self._stop()

    def start(self):
        with self.op_lock:
            self._stop()
            self._start()

    def set_frame_data(self, frame_data):
        # frame_data = (image_data, time) or
        # frame_data = callable()->(image_data, time)
        with self.lock:
            self.frame_data_raw = frame_data

    def get_frame_data(self):
        with self.lock:
            frame_data = self.frame_data_raw
            self.frame_data_raw = None  # handled
            if callable(frame_data):
                self.frame_data = frame_data()
            elif frame_data is not None:
                self.frame_data = frame_data

            return self.frame_data

    def get_fps(self):
        tid_times = defaultdict(lambda: [])
        with self.op_lock:
            for tid, t in self.fps_counter:
                tid_times[tid].append(t)

        fps = []
        for tid, times in tid_times.items():
            prev = None
            diff = []
            for t in times:
                if prev is not None:
                    diff.append(t - prev)
                prev = t
            if diff:
                fps.append(1.0 / (sum(diff) / len(diff)))
        if fps:
            return sum(fps) / len(fps)
        else:
            return 0

    def _tensor_to_ndarray(self, frame):
        if isinstance(frame, torch.Tensor):
            if frame.device.type != "cpu":
                frame = frame.detach().to("cpu")
            else:
                frame = frame.detach()
            frame = frame.clamp(0.0, 1.0)
            if frame.ndim == 3:
                frame = frame.permute(1, 2, 0)
            elif frame.ndim == 4:
                frame = frame.squeeze(0).permute(1, 2, 0)
            frame = (frame * 255.0).round().to(torch.uint8)
            return frame.numpy()
        if isinstance(frame, np.ndarray):
            if frame.dtype != np.uint8:
                arr = np.clip(frame, 0.0, 1.0)
                arr = (arr * 255.0).round().astype(np.uint8)
                return arr
            return frame
        raise TypeError(f"Unsupported frame type: {type(frame)}")

    def _create_video_stream(self, container, width, height):
        codec_candidates = ["libx264", "h264", "mpeg4"]
        last_error = None
        for codec in codec_candidates:
            try:
                stream = container.add_stream(codec, rate=self.fps)
                stream.width = width
                stream.height = height
                if self.fps:
                    stream.time_base = Fraction(1, int(self.fps))
                if codec in ("libx264", "h264"):
                    stream.pix_fmt = "yuv420p"
                    stream.options = {
                        "preset": "veryfast",
                        "tune": "zerolatency",
                        "crf": "23",
                    }
                else:
                    stream.pix_fmt = "yuv420p"
                return stream
            except av.AVError as exc:  # pragma: no cover - codec availability differs per environment
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("Unable to initialize video encoder")

    def send_video_stream(self, start_response):
        def gen():
            generator_id = random.getrandbits(64)
            data_tick = 0
            buffer = _StreamingBuffer()
            container = None
            stream = None
            pts = 0
            time_base = Fraction(1, int(self.fps)) if self.fps else Fraction(1, 30)

            try:
                while True:
                    try:
                        data = self.get_frame_data()
                        if data is not None:
                            frame, tick = data
                            if tick <= data_tick:
                                # already handled
                                pass
                            else:
                                data_tick = tick
                                with self.op_lock:
                                    self.fps_counter.append((generator_id, time.perf_counter()))

                                ndarray = self._tensor_to_ndarray(frame)
                                if container is None:
                                    container = av.open(
                                        buffer,
                                        mode="w",
                                        format="mp4",
                                        options={
                                            "movflags": "frag_keyframe+empty_moov+default_base_moof",
                                            "brand": "iso6"
                                        }
                                    )
                                    stream = self._create_video_stream(container, ndarray.shape[1], ndarray.shape[0])
                                    container.write_header()
                                    header = buffer.read_new()
                                    if header:
                                        yield header

                                video_frame = av.VideoFrame.from_ndarray(ndarray, format="rgb24")
                                video_frame.pts = pts
                                video_frame.time_base = time_base
                                pts += 1

                                for packet in stream.encode(video_frame):
                                    packet.time_base = stream.time_base
                                    container.mux(packet)

                                chunk = buffer.read_new()
                                if chunk:
                                    yield chunk

                        if self.shutdown_event.is_set():
                            break

                        if data is None:
                            time.sleep(1 / 1000)
                    except GeneratorExit:
                        raise
                    except Exception:  # noqa: broad-except
                        print("StreamingServer", sys.exc_info(), file=sys.stderr)
                        raise
            finally:
                if container is not None:
                    for packet in stream.encode(None):
                        container.mux(packet)
                    container.close()
                    tail = buffer.read_new()
                    if tail:
                        yield tail

            yield b""

        headers = [
            ("Content-Type", self.stream_content_type),
            ("Cache-Control", "no-cache, no-store, must-revalidate"),
            ("Pragma", "no-cache"),
            ("Expires", "0"),
            ("X-Content-Duration", str(self.fake_duration))
        ]
        start_response(STATUS_OK, headers)
        return gen()

    def send_index(self, start_response):
        template = Template(self.index_template)
        page_data = template.substitute(
            frame_width=self.frame_width,
            frame_height=self.frame_height,
            fps=self.fps,
            stream_uri=self.stream_uri
        ).encode()
        start_response(STATUS_OK, [('Content-type', "text/html; charset=utf-8")])
        return [page_data]

    def send_404(self, start_response):
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"404 Not Found"]

    def send_process_token(self, start_response):
        start_response(STATUS_OK, [("Content-Type", "application/json; charset=utf-8")])
        return [json.dumps({"token": self.process_token}).encode()]

    def handle(self, environ, start_response):
        uri = environ['PATH_INFO']
        #  print("request", uri)

        if self.auth is not None:
            # HTTP Basic Authentication
            auth = environ.get("HTTP_AUTHORIZATION", "")
            #  print("auth", auth)
            if auth != self.auth:
                start_response(
                    "401 Unauthorized",
                    [("WWW-Authenticate", "Basic charset=utf-8"),
                     ("Content-Type", "text/plain; charset=utf-8")])
                return [b"Authorization Required"]

        if uri == "/":
            return self.send_index(start_response)
        elif uri == "/process_token":
            return self.send_process_token(start_response)
        elif uri == self.stream_uri:
            return self.send_video_stream(start_response)
        else:
            return self.send_404(start_response)
