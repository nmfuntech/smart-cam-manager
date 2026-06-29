import os

import cv2
from dotenv import load_dotenv

load_dotenv()

# Force RTSP over TCP for more reliable LAN streaming.
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

username = os.getenv("TAPO_USERNAME")
password = os.getenv("TAPO_PASSWORD")
host = os.getenv("TAPO_HOST", "192.168.1.50")
port = os.getenv("TAPO_RTSP_PORT", "554")
stream_path = os.getenv("TAPO_STREAM_PATH", "stream1")

if not username or not password:
    raise RuntimeError(
        "Credenziali mancanti. Imposta TAPO_USERNAME e TAPO_PASSWORD nel file .env"
    )

rtsp_url = f"rtsp://{username}:{password}@{host}:{port}/{stream_path}"

cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)

if not cap.isOpened():
    raise RuntimeError("Impossibile aprire lo stream RTSP")

while True:
    ok, frame = cap.read()
    if not ok:
        print("Frame non ricevuto")
        break

    cv2.imshow("Tapo RTSP", frame)

    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

cap.release()
cv2.destroyAllWindows()
