# Python version of CameraAccess Plus application that is no longer working

**Not affiliated with or endorsed by Canon Inc.**  
© 2025 jocxfin. All rights reserved.

## Overview  
Lightweight Python client for Canon LEGRIA/VIXIA cameras over Wi‑Fi, featuring:  
- Resilient HLS playback with automatic reconnect  
- Precise output via high‑precision timer  
- ONVIF PTZ support (ContinuousMove, GotoPreset, Stop)  
- Virtual presets emulation  
- Support for ONVIF PTZ Frigate integration

## Tech Stack  
- **Language**: Python 3.10+  
- **HTTP**: `requests`  
- **Video**: OpenCV (`cv2`) + FFmpeg (external)  
- **Server**: Flask + Waitress  
- **Streaming broker**: MediaMTX 
- **ONVIF**: SOAP via XML + embedded WSDLs  

## Prerequisites  
- Python 3.10+  
- FFmpeg (system package or static build)  
- MediaMTX binary (download separately)  
- Host PC connected to camera's Wi‑Fi network

## Installation  
```bash
git clone https://github.com/jocxfin/LegriaStream.git
cd LegriaStream
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
````

Download MediaMTX and place its config at `mediamtx.yml`.

## Configuration

* Edit `main.py` or set env vars(usually not needed, as the defaults are sufficient):

  * `CAMERA_IP`, `CAMERA_PORT`
  * `RESTREAM_PORT`, `ONVIF_PORT`
* WSDLs located in `onvif/wsdl/`

## Usage

```bash
python3 main.py [--novid]
```

* `--novid`: headless mode (no OpenCV window).
* Access RTSP at `rtsp://localhost:8424/live/stream`.
* ONVIF SOAP endpoint: `http://<host>:9858/onvif/device_service`

## License

This project is MIT‑licensed. See [LICENSE](LICENSE).

## Disclaimer & Legal

* Reverse‑engineered under EU Software Directive 2009/24/EC for interoperability.
* No Canon firmware or binaries included.
* Use at your own risk. No warranty.

