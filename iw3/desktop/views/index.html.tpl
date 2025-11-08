<!DOCTYPE html>
<html>
<head>
  <title>iw3 desktop streaming</title>
  <script>
    const FPS = ${fps};
    const WIDTH = ${frame_width};
    const HEIGHT = ${frame_height};
    const STREAM_URI = "${stream_uri}";

    window.onload = () => {
        let process_token = null;
        let stop_update = false;
        const video = document.getElementById("player-video");

        function appendTimestamp(url) {
            const ts = Date.now();
            return url.includes('?') ? `${url}&ts=${ts}` : `${url}?ts=${ts}`;
        }

        function startPlayback() {
            video.src = appendTimestamp(STREAM_URI);
            const playPromise = video.play();
            if (playPromise !== undefined) {
                playPromise.catch(() => {});
            }
        }

        function setup_interval() {
            setInterval(() => {
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
                        } else if (process_token != res.token) {
                            process_token = null;
                            location.reload();
                        }
                    })
                    .catch(() => {
                        stop_update = true;
                    });
            }, 4000);
        }

        video.addEventListener('error', () => {
            if (!stop_update) {
                setTimeout(startPlayback, 1000);
            }
        });

        startPlayback();
        setup_interval();
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
    <video id="player-video" class="video" controls controlsList="nodownload"
            autoplay muted poster="" disablepictureinpicture playsinline>
    </video>
  </div>
</body>
</html>
