"""HTTP streaming server that serves a fragmented MP4 stream."""
import base64
import io
import json
import random
import sys
import threading
import time
from collections import defaultdict, deque
from fractions import Fraction
from socketserver import ThreadingMixIn
from string import Template
from wsgiref.simple_server import WSGIServer, make_server

import av
import torch


STATUS_OK = "200 OK"


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True
    block_on_close = False


class _ChunkBuffer(io.RawIOBase):
    """Collects bytes written by PyAV and exposes them as chunks."""

    def __init__(self):
        super().__init__()
        self._chunks = deque()
        self._condition = threading.Condition()
        self._closed = False

    def writable(self):
        return True

    def write(self, b):
        if not b:
            return 0
        with self._condition:
            self._chunks.append(bytes(b))
            self._condition.notify_all()
        return len(b)

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        super().close()

    def pop(self, timeout=None):
        with self._condition:
            if not self._chunks and not self._closed:
                self._condition.wait(timeout)
            if self._chunks:
                return self._chunks.popleft()
            return None

    def pop_nowait(self):
        with self._condition:
            if self._chunks:
                return self._chunks.popleft()
            return None


class StreamingServer():
    def __init__(
            self, port, lock,
            frame_width, frame_height, fps,
            index_template,
            stream_uri="/stream.mp4", stream_content_type="video/mp4",
            auth=None, host="", stream_quality=90):
        self.port = port
        self.host = host
        self.lock = lock
        self.op_lock = threading.Lock()
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.fps = fps
        self.index_template = index_template
        self.stream_uri = stream_uri
        self.stream_content_type = stream_content_type
        self.stream_quality = stream_quality

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
        # frame_data = (frame, time) or
        # frame_data = callable()->(frame, time)
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

    def _quality_to_crf(self, quality):
        quality = max(1, min(100, int(quality)))
        min_crf = 18
        max_crf = 40
        span = max_crf - min_crf
        return max_crf - int(round((quality - 1) * (span / 99)))

    def _tensor_to_video_frame(self, frame_tensor):
        frame = frame_tensor.detach()
        if frame.device.type != "cpu":
            frame = frame.to("cpu")
        frame = frame.clamp(0.0, 1.0)
        frame = (frame * 255.0).to(torch.uint8)
        frame = frame.permute(1, 2, 0).contiguous()
        video_frame = av.VideoFrame.from_ndarray(frame.numpy(), format="rgb24")
        return video_frame.reformat(
            width=self.frame_width,
            height=self.frame_height,
            format="yuv420p"
        )

    def send_video_stream(self, start_response):
        def gen():
            generator_id = random.getrandbits(64)
            data_tick = 0
            chunk_buffer = _ChunkBuffer()
            container = av.open(
                chunk_buffer,
                mode="w",
                format="mp4",
                options={
                    "movflags": "empty_moov+default_base_moof+frag_keyframe",
                }
            )

            stream = container.add_stream("libx264", rate=self.fps)
            stream.width = self.frame_width
            stream.height = self.frame_height
            stream.pix_fmt = "yuv420p"
            stream.time_base = Fraction(1, self.fps)
            stream.options = {
                "preset": "ultrafast",
                "tune": "zerolatency",
                "crf": str(self._quality_to_crf(self.stream_quality)),
            }
            stream.codec_context.max_b_frames = 0
            stream.codec_context.time_base = Fraction(1, self.fps)
            stream.codec_context.profile = "baseline"

            try:
                while True:
                    try:
                        chunk = chunk_buffer.pop(timeout=0.01)
                        if chunk:
                            yield chunk
                            continue

                        data = self.get_frame_data()
                        if data is not None:
                            frame, tick = data
                            if tick > data_tick:
                                data_tick = tick
                                with self.op_lock:
                                    self.fps_counter.append((generator_id, time.perf_counter()))
                                if isinstance(frame, tuple):
                                    frame = frame[0]
                                if isinstance(frame, (bytes, bytearray)):
                                    raise RuntimeError("Byte frames are no longer supported by the video stream")
                                video_frame = self._tensor_to_video_frame(frame)
                                for packet in stream.encode(video_frame):
                                    container.mux(packet)
                                continue

                        if self.shutdown_event.is_set():
                            break

                        time.sleep(1 / 1000)
                    except GeneratorExit:
                        break
                    except Exception:
                        print("StreamingServer", sys.exc_info(), file=sys.stderr)
                        raise

                for packet in stream.encode():
                    container.mux(packet)
            finally:
                container.close()
                while True:
                    chunk = chunk_buffer.pop_nowait()
                    if not chunk:
                        break
                    yield chunk

        start_response(
            STATUS_OK,
            [("Content-Type", self.stream_content_type)])
        return gen()

    def send_index(self, start_response):
        template = Template(self.index_template)
        page_data = template.substitute(
            frame_width=self.frame_width,
            frame_height=self.frame_height,
            fps=self.fps,
            stream_uri=self.stream_uri
        ).encode()
        start_response(STATUS_OK, [("Content-type", "text/html; charset=utf-8")])
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
