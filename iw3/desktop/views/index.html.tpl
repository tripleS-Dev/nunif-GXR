<!DOCTYPE html>
<html>
<head>
  <title>iw3 desktop streaming</title>
  <script>
    const WIDTH = ${frame_width};
    const HEIGHT = ${frame_height};
    const STREAM_URI = "${stream_uri}";
    const ARBITRARY_DURATION = 3600; // seconds
    const BUFFER_WINDOW = 30; // seconds of history to keep buffered

    function appendBufferAsync(sourceBuffer, data) {
        return new Promise((resolve, reject) => {
            const tryAppend = () => {
                if (sourceBuffer.updating) {
                    sourceBuffer.addEventListener("updateend", tryAppend, { once: true });
                    return;
                }
                const cleanup = () => {
                    sourceBuffer.removeEventListener("updateend", onUpdate);
                    sourceBuffer.removeEventListener("error", onError);
                };
                const onUpdate = () => {
                    cleanup();
                    resolve();
                };
                const onError = (event) => {
                    cleanup();
                    reject(event);
                };
                sourceBuffer.addEventListener("updateend", onUpdate);
                sourceBuffer.addEventListener("error", onError);
                try {
                    sourceBuffer.appendBuffer(data);
                } catch (err) {
                    cleanup();
                    reject(err);
                }
            };
            tryAppend();
        });
    }

    window.onload = () => {
        const video = document.getElementById("player-canvas");
        let process_token = null;
        let stop_update = false;
        let sourceBuffer = null;

        const mediaSource = new MediaSource();
        video.src = URL.createObjectURL(mediaSource);
        video.width = WIDTH;
        video.height = HEIGHT;

        const pruneBuffer = () => {
            if (!sourceBuffer || sourceBuffer.updating) {
                return;
            }
            if (sourceBuffer.buffered.length === 0) {
                return;
            }
            const currentTime = video.currentTime;
            const removeEnd = currentTime - BUFFER_WINDOW;
            if (removeEnd <= 0) {
                return;
            }
            const start = sourceBuffer.buffered.start(0);
            if (removeEnd > start) {
                try {
                    sourceBuffer.remove(0, removeEnd);
                } catch (err) {
                    console.warn("Failed to prune buffer", err);
                }
            }
        };

        mediaSource.addEventListener("sourceopen", async () => {
            const mimeCodec = 'video/mp4; codecs="avc1.640028"';
            if (!MediaSource.isTypeSupported(mimeCodec)) {
                console.error("Unsupported MIME type", mimeCodec);
                stop_update = true;
                return;
            }
            sourceBuffer = mediaSource.addSourceBuffer(mimeCodec);
            mediaSource.duration = ARBITRARY_DURATION;

            try {
                const response = await fetch(STREAM_URI);
                if (!response.ok) {
                    throw new Error(`Failed to fetch stream: ${response.status}`);
                }
                const reader = response.body.getReader();
                const readChunk = async () => {
                    const { value, done } = await reader.read();
                    if (done) {
                        if (mediaSource.readyState === "open") {
                            mediaSource.endOfStream();
                        }
                        return;
                    }
                    const chunk = value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength);
                    try {
                        await appendBufferAsync(sourceBuffer, chunk);
                    } catch (err) {
                        if (err && err.name === 'QuotaExceededError') {
                            pruneBuffer();
                            await appendBufferAsync(sourceBuffer, chunk);
                        } else {
                            throw err;
                        }
                    }
                    pruneBuffer();
                    readChunk();
                };
                readChunk();
            } catch (err) {
                stop_update = true;
                console.error("Stream error", err);
            }
        });

        video.play().catch((err) => {
            console.warn("Autoplay failed", err);
        });

        const checkToken = () => {
            if (stop_update || document.hidden) {
                return;
            }
            fetch('/process_token', { method: 'GET' })
                .then((res) => {
                    if (!res.ok) {
                        stop_update = true;
                    }
                    return res.json();
                })
                .then((res) => {
                    if (process_token == null) {
                        process_token = res.token;
                    } else if (process_token !== res.token) {
                        process_token = null;
                        location.reload();
                    }
                })
                .catch((reason) => {
                    stop_update = true;
                    console.error("Token check failed", reason);
                });
        };

        setInterval(checkToken, 4000);
    };
  </script>
  <style type="text/css">
  body {
    margin: 0;
  }
  .video-container {
    position: fixed;
    left: 0;
    top: 0;
    z-index: 0;
    width: 100vw;
    height: 100vh;
    text-align: center;
    background-color: rgb(45, 48, 53);
  }
  .video {
    height: 100%;
    width: 100%;
    object-fit: contain;
    object-position: center;
  }
  </style>
</head>
<body>
  <div class="video-container">
    <video id="player-canvas" class="video" controls controlsList="nodownload"
            autoplay muted poster="" disablepictureinpicture >
    </video>
  </div>
</body>
</html>
