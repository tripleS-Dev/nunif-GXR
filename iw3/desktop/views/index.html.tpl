<!DOCTYPE html>
<html>
<head>
  <title>iw3 desktop streaming</title>
  <script>
    const STREAM_URI = "${stream_uri}";
    const RELOAD_INTERVAL_MS = 4000;

    window.onload = () => {
        const video = document.getElementById("player-canvas");
        let process_token = null;

        function setVideoSource() {
            const cacheBuster = `?_ts=${Date.now()}`;
            const sourceUrl = STREAM_URI + cacheBuster;
            video.src = sourceUrl;
            const playPromise = video.play();
            if (playPromise !== undefined) {
                playPromise.catch(() => {
                    /* ignored */
                });
            }
        }

        setVideoSource();

        video.addEventListener('error', () => {
            setTimeout(setVideoSource, 1000);
        });

        setInterval(() => {
            if (document.hidden) {
                return;
            }
            fetch('/process_token', { method: 'GET' })
                .then((res) => {
                    if (!res.ok) {
                        throw new Error('unavailable');
                    }
                    return res.json();
                })
                .then((res) => {
                    if (process_token == null) {
                        process_token = res.token;
                    } else if (process_token !== res.token) {
                        process_token = null;
                        setVideoSource();
                    }
                })
                .catch(() => {
                    setVideoSource();
                });
        }, RELOAD_INTERVAL_MS);
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
            autoplay muted poster="" disablepictureinpicture playsinline>
    </video>
  </div>
</body>
</html>
