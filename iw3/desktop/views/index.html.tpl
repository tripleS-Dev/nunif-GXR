<!DOCTYPE html>
<html>
<head>
  <title>iw3 desktop streaming</title>
  <script>
    const STREAM_URI = "${stream_uri}";
    const FALLBACK_DURATION_SECONDS = 24 * 60 * 60;

    window.addEventListener("load", () => {
        const video = document.getElementById("player-video");
        let processToken = null;

        function attachStream() {
            const source = document.createElement("source");
            source.src = `${STREAM_URI}?t=${Date.now()}`;
            source.type = "video/mp4";
            while (video.firstChild) {
                video.removeChild(video.firstChild);
            }
            video.appendChild(source);
            video.load();
            video.play().catch(() => {
                // Autoplay might be blocked; the controls let the user start playback manually.
            });
        }

        function monitorProcessToken() {
            if (document.hidden) {
                return;
            }
            fetch('/process_token')
                .then((res) => {
                    if (!res.ok) {
                        throw new Error(res.statusText);
                    }
                    return res.json();
                })
                .then((res) => {
                    if (processToken === null) {
                        processToken = res.token;
                    } else if (processToken !== res.token) {
                        processToken = null;
                        attachStream();
                    }
                })
                .catch(() => {
                    // Ignore transient errors; the player will continue using the current stream.
                });
        }

        function applyDurationFallback() {
            if (!Number.isFinite(video.duration) || video.duration === Infinity) {
                try {
                    Object.defineProperty(video, "duration", {
                        configurable: true,
                        get() {
                            return FALLBACK_DURATION_SECONDS;
                        },
                    });
                } catch (err) {
                    video.dataset.fallbackDuration = String(FALLBACK_DURATION_SECONDS);
                }
            }
        }

        video.addEventListener("loadedmetadata", applyDurationFallback);

        attachStream();
        setInterval(monitorProcessToken, 4000);
    });
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
    <video id="player-video" class="video" controls controlsList="nodownload"
            autoplay muted playsinline poster="" disablepictureinpicture preload="auto">
    </video>
  </div>
</body>
</html>
