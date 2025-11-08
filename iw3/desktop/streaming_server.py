"""HTTP streaming server that delivers frames as a fragmented MP4 stream."""
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
import torch
import av


STATUS_OK = "200 OK"


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True
    block_on_close = False


class StreamingServer():
    def __init__(
            self, port, lock,
            frame_width, frame_height, fps,
            index_template,
            stream_uri="/stream.mp4", stream_content_type="video/mp4",
            video_quality=23,
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
        self.video_quality = int(video_quality)
        self.fake_duration = self.fps * 24 * 3600

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

    def send_video_stream(self, start_response):
        def to_rgb_array(frame):
            if isinstance(frame, torch.Tensor):
                frame = frame.detach()
                if frame.device.type != "cpu":
                    frame = frame.cpu()
                frame = frame.clamp(0, 1)
                frame = frame.mul(255).round().to(torch.uint8)
                frame = frame.permute(1, 2, 0).contiguous()
                return frame.numpy()
            if isinstance(frame, np.ndarray):
                if frame.dtype != np.uint8:
                    frame = np.clip(frame, 0, 1)
                    frame = np.multiply(frame, 255.0).round().astype(np.uint8)
                if frame.shape[-1] == 4:
                    frame = frame[..., :3]
                return frame
            raise TypeError(f"Unsupported frame type: {type(frame)}")

        class QueueWriter(io.RawIOBase):
            def __init__(self):
                super().__init__()
                self._chunks = deque()
                self._lock = threading.Lock()

            def writable(self):
                return True

            def write(self, b):
                if b:
                    with self._lock:
                        self._chunks.append(bytes(b))
                return len(b)

            def pop(self):
                with self._lock:
                    if self._chunks:
                        return self._chunks.popleft()
                return None

        def gen():
            generator_id = random.getrandbits(64)
            writer = QueueWriter()
            options = {
                "movflags": "empty_moov+default_base_moof+frag_keyframe",
            }
            container = av.open(writer, mode="w", format="mp4", options=options)
            stream = container.add_stream(
                "libx264",
                rate=self.fps,
                options={
                    "preset": "ultrafast",
                    "tune": "zerolatency",
                    "crf": str(self.video_quality),
                },
            )
            stream.width = self.frame_width
            stream.height = self.frame_height
            stream.pix_fmt = "yuv420p"
            stream.time_base = Fraction(1, self.fps)
            stream.gop_size = max(1, self.fps)
            stream.duration = self.fake_duration  # fake 24 hour duration
            codec_ctx = stream.codec_context
            codec_ctx.max_b_frames = 0

            container.write_header()
            while True:
                chunk = writer.pop()
                if chunk is None:
                    break
                yield chunk

            pts = 0
            last_tick = 0
            try:
                while True:
                    data = self.get_frame_data()
                    if data is not None:
                        frame, tick = data
                        if tick > last_tick:
                            last_tick = tick
                            with self.op_lock:
                                self.fps_counter.append((generator_id, time.perf_counter()))
                            video_frame = av.VideoFrame.from_ndarray(to_rgb_array(frame), format="rgb24")
                            video_frame.time_base = stream.time_base
                            video_frame.pts = pts
                            pts += 1
                            for packet in stream.encode(video_frame):
                                container.mux(packet)
                            while True:
                                chunk = writer.pop()
                                if chunk is None:
                                    break
                                yield chunk
                    if self.shutdown_event.is_set():
                        break
                    time.sleep(1 / 1000)
            except GeneratorExit:
                raise
            except Exception:  # noqa
                print("StreamingServer", sys.exc_info(), file=sys.stderr)
                raise
            finally:
                for packet in stream.encode():
                    container.mux(packet)
                container.close()
                while True:
                    chunk = writer.pop()
                    if chunk is None:
                        break
                    yield chunk
                yield b""

        start_response(
            STATUS_OK,
            [("Content-Type", self.stream_content_type),
             ("Cache-Control", "no-store, max-age=0"),
             ("Pragma", "no-cache")])
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
