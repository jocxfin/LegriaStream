import logging, os, queue, threading, time, uuid, xml.etree.ElementTree as ET
import subprocess
from urllib.parse import urljoin
import cv2, requests
from flask import Flask, request, Response, send_from_directory
from waitress import serve
import argparse, socket
import tempfile
import re

CAMERA_IP, CAMERA_PORT = "192.168.0.80", 80
USER_AGENT = "canonStreamingApp/1.0.0"
TARGET_FPS = 22.92
RESTREAM_PORT = 8424
RESTREAM_PATH = "live/stream"
ONVIF_PORT = 9858
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720

info_handler = logging.StreamHandler()
info_handler.setLevel(logging.INFO)
info_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logging.getLogger('werkzeug').setLevel(logging.WARNING)
logging.getLogger('waitress').setLevel(logging.WARNING)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(info_handler)
class HLS:
    """Download HLS segments and queue them."""
    def __init__(self, sess: requests.Session, master_url: str):
        self.s = sess
        self.master = master_url
        self.q = queue.Queue(maxsize=10)
        self.dead = False
        self.t = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.t.start()
    def stop(self):
        self.dead = True
        self.t.join(2)
    def get(self, to=2):
        try:
            return self.q.get(timeout=to)
        except queue.Empty:
            return None

    def _loop(self):
        playlist_url = None
        while not self.dead:
            try:
                if not playlist_url:
                    r = self.s.get(self.master, headers={"Range": "bytes=0-"}, timeout=5)
                    r.raise_for_status()
                    text = r.text.strip()
                    if '#EXT-X-STREAM-INF' in text:
                        for ln in text.splitlines():
                            if ln and not ln.startswith("#"):
                                playlist_url = urljoin(self.master, ln.strip())
                                break
                    else:
                        playlist_url = self.master
                    if not playlist_url:
                        raise RuntimeError("No HLS variant found")
                    logger.info(f"Using variant playlist: {playlist_url}")

                seen = set()
                while not self.dead:
                    try:
                        r = self.s.get(playlist_url, headers={"Range": "bytes=0-"}, timeout=5)
                        r.raise_for_status()
                        lines = r.text.splitlines()
                        seg_dur = 0
                        for line in lines:
                            line = line.strip()
                            if line.startswith("#EXTINF:"):
                                try:
                                    seg_dur = float(line.split(":")[1].split(",")[0])
                                except:
                                    seg_dur = 0.5
                            elif line and not line.startswith("#"):
                                if line in seen:
                                    continue
                                seg_url = line if line.startswith("http") else urljoin(playlist_url, line)
                                try:
                                    data = self.s.get(seg_url, headers={"Range": "bytes=0-"}, timeout=10).content
                                    if len(data) > 500:
                                        self.q.put((data, seg_dur), timeout=5)
                                        seen.add(line)
                                except queue.Full:
                                    logger.warning("Segment queue is full; pausing download")
                                    time.sleep(0.5)
                                except Exception as e:
                                    logger.warning(f"Failed to fetch segment {line}: {e}")
                        if len(seen) > 300:
                            seen = set(list(seen)[-150:])
                        time.sleep(0.2)
                    except Exception as e:
                        logger.warning(f"Playlist fetch error: {e}. Retrying in 2s.")
                        time.sleep(2)
            except Exception as e:
                logger.error(f"HLS thread error: {e}. Resetting in 10s.")
                playlist_url = None
                time.sleep(10)
        self.q.put(None)

class Cam:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "keep-alive",
            "Accept-Encoding": "identity", "Cache-Control": "no-cache", "Pragma": "no-cache"
        })
        self.ctrl, self.play = "", ""
        self.stream = None
        self.session_id = ""
        self.remote_handle = 0
        self.frame_queue = queue.Queue(maxsize=int(TARGET_FPS * 10))
        self.decoder_thread = None
        self.stop_event = threading.Event()
        self.novid = False
        self.ffmpeg_proc = None
        self.mtx_proc = None
        self.onvif_thread = None
        self.status_lock = threading.Lock()
        self.actual_fps = 0.0
        self.avg_processing_time = 0.0
        self.avg_sleep_time = 0.0
        self.current_zoom_pos = -1
        self.zoom_status = "Idle"

        self.presets = {
            f"{i}": {"name": f"Zoom-{i}", "pos": i} for i in range(1, 12)
        }
        self.goto_preset_thread = None
        self.zoom_thread = None
        self.zoom_stop_event = threading.Event()

    def connect(self, ip=CAMERA_IP, port=CAMERA_PORT):
        logger.info("=== CONNECTING TO CAMERA ===")
        device_xml = f"http://{ip}:{port}/dev/device.xml"
        try:
            resp = self.s.get(device_xml, timeout=10)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            namespace = {'upnp': 'urn:schemas-upnp-org:device-1-0'}
            ctrl = root.findtext('.//upnp:X_CctrlURL', namespaces=namespace)
            play = root.findtext('.//upnp:X_CplayListURL', namespaces=namespace)
            if ctrl and play:
                base = f"http://{ip}:{port}"
                self.ctrl = ctrl if ctrl.startswith("http") else urljoin(base, ctrl)
                self.play = play if play.startswith("http") else urljoin(base, play)
                logger.info(f"Control URL: {self.ctrl}")
                logger.info(f"Playlist URL: {self.play}")
            else:
                logger.error("Device XML missing control/playlist URLs")
                return False
        except Exception as e:
            logger.error(f"Failed to read device.xml: {e}")
            return False

        self.session_id = uuid.uuid4().hex
        self.s.headers["x-session-id"] = self.session_id
        logger.info(f"Session ID: {self.session_id}")

        if not self._simulate_set_first_playlist_notify(): return False
        if not self._simulate_init_libcurl(): return False
        if not self._wait_camera_ready(): return False
        if not self._simulate_remote_init(): return False

        logger.info("Camera initialized and ready")
        self._log_camera_info()
        return True

    def _simulate_set_first_playlist_notify(self):
        logger.info("Step 2: Registering playlist callback")
        try:
            r = self.s.get(self.play, headers={"Range": "bytes=0-"}, timeout=5)
            if r.status_code == 200 and r.text.strip().startswith('#EXTM3U'):
                logger.info("Playlist reachable")
                return True
            logger.error(f"Playlist access error: HTTP {r.status_code}")
        except Exception as e:
            logger.error(f"SetFirstPlaylistNotify simulation failed: {e}")
        return False

    def _simulate_init_libcurl(self):
        logger.info("Step 3: Initializing HTTP client")
        for ep in ["/getinfo", "/getcurprop"]:
            try:
                url = urljoin(self.ctrl, ep)
                code = self.s.get(url, params={'seq': 0}, timeout=3).status_code
                if code in (200, 400, 404, 500):
                    logger.info("✓ HTTP client ready")
                    return True
            except: continue
        logger.error("InitLibCurl simulation failed")
        return False

    def _wait_camera_ready(self):
        logger.info("Step 4: Waiting for camera readiness")
        for i in range(1, 11):
            try:
                if self.s.get(urljoin(self.ctrl, "getinfo"), timeout=3).status_code == 200:
                    logger.info(f"✓ Camera responded on attempt {i}")
                    return True
            except:
                time.sleep(min(0.5 * i, 4))
        logger.warning("Camera readiness timed out; proceeding anyway")
        return True

    def _simulate_remote_init(self):
        logger.info("Step 5: Simulating RemoteControllerInit")
        try:
            resp = self.s.get(self.play, headers={"Range": "bytes=0-"}, timeout=5)
            if resp.status_code == 200 and len(resp.content) > 100:
                logger.info("✓ Playlist valid, remote controller initialized")
                self.remote_handle = 1
                self._start_property_polling()
                return True
            logger.error(f"Playlist invalid: HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"RemoteControllerInit failed: {e}")
        return False

    def _start_property_polling(self):
        def poll_loop():
            seq = 0
            while not self.stop_event.is_set():
                try:
                    ok, props = self._send_cmd("getcurprop", {'seq': seq})
                    if ok and props:
                        seq = props.get('seq', seq) + 1
                        with self.status_lock:
                            self.current_zoom_pos = props.get('zoompos', -1)
                    else:
                        time.sleep(2)
                except:
                    pass
                with self.status_lock:
                    is_zooming = self.zoom_status != "Idle"
                
                if is_zooming:
                    time.sleep(0.1)
                else:
                    time.sleep(0.5)
        threading.Thread(target=poll_loop, daemon=True).start()

    def _send_cmd(self, endpoint, params):
        if not self.ctrl: return False, "No control URL"
        try:
            url = urljoin(self.ctrl+'/', endpoint)
            r = self.s.get(url, params=params, timeout=2)
            r.raise_for_status()
            return True, r.json() if r.text else None
        except Exception as e:
            logger.error(f"Camera command {endpoint} failed: {e}")
            return False, str(e)

    def _log_camera_info(self):
        logger.info("=== Camera Diagnostics ===")
        ok, info = self._send_cmd("getinfo", {})
        if ok: logger.info(f"Camera Info: {info}")
        ok, props = self._send_cmd("getcurprop", {'seq': 0})
        if ok: logger.info(f"Camera Properties: {props}")
        logger.info("==========================")

    def _continuous_zoom(self, action):
        with self.status_lock:
            self.zoom_status = "In" if action == 'tele1' else "Out"
        while not self.zoom_stop_event.is_set():
            self._send_cmd("drivelens", {'zoom': action})
            time.sleep(0.25)
        with self.status_lock:
            self.zoom_status = "Idle"

    def _goto_preset_worker(self, preset_name, target_pos):
        with self.status_lock:
            self.zoom_status = f"Preset: {preset_name}"
        try:
            for _ in range(60):
                with self.status_lock:
                    current_pos = self.current_zoom_pos
                
                if current_pos == -1:
                    time.sleep(0.25)
                    continue

                if current_pos == target_pos:
                    logger.info("GotoPreset: Target position reached.")
                    self._send_cmd("drivelens", {'zoom': 'stop'})
                    return
                if target_pos == 1 or target_pos == 11:
                    direction = 'tele3' if current_pos < target_pos else 'wide3'
                else:
                    direction = 'tele2' if current_pos < target_pos else 'wide2'

                self._send_cmd("drivelens", {'zoom': direction})
                time.sleep(0.25)

            logger.warning("GotoPreset worker timed out.")
        except Exception as e:
            logger.error(f"Error in GotoPreset worker: {e}")
        finally:
            self._send_cmd("drivelens", {'zoom': 'stop'})
            with self.status_lock:
                self.zoom_status = "Idle"

    def start_onvif_server(self):
        if self.onvif_thread and self.onvif_thread.is_alive(): return
        app = Flask(__name__)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('10.255.255.255', 1)); local_ip = s.getsockname()[0]
        except: local_ip = '127.0.0.1'
        finally: s.close()
        base = f"http://{local_ip}:{ONVIF_PORT}"

        @app.route('/onvif/wsdl/<path:filename>')
        def wsdl_files(filename): return send_from_directory('onvif/wsdl', filename)
        
        @app.route('/onvif/device_service', methods=['GET','POST'])
        def device_service():
            if 'wsdl' in request.args: return send_from_directory('onvif/wsdl', 'device.wsdl')
            body = request.data
            if b'GetCapabilities' in body:
                body_str = body.decode('utf-8', errors='ignore')
                capabilities_xml = ""
                if '<tds:Category>PTZ</tds:Category>' in body_str:
                    logger.info("ONVIF: Received GetCapabilities request for PTZ.")
                    capabilities_xml = f'<tt:PTZ xmlns:tt="http://www.onvif.org/ver10/schema"><tt:XAddr>{base}/onvif/ptz_service</tt:XAddr></tt:PTZ>'
                elif '<tds:Category>Media</tds:Category>' in body_str:
                    logger.info("ONVIF: Received GetCapabilities request for Media.")
                    capabilities_xml = f'<tt:Media xmlns:tt="http://www.onvif.org/ver10/schema"><tt:XAddr>{base}/onvif/media_service</tt:XAddr></tt:Media>'
                else:
                    logger.info("ONVIF: Received GetCapabilities request for All.")
                    capabilities_xml = f"""<tt:Media xmlns:tt="http://www.onvif.org/ver10/schema"><tt:XAddr>{base}/onvif/media_service</tt:XAddr></tt:Media><tt:PTZ xmlns:tt="http://www.onvif.org/ver10/schema"><tt:XAddr>{base}/onvif/ptz_service</tt:XAddr></tt:PTZ>"""
                response_xml = f"""<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"><SOAP-ENV:Body><tds:GetCapabilitiesResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><tds:Capabilities>{capabilities_xml}</tds:Capabilities></tds:GetCapabilitiesResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>"""
                return Response(response_xml, mimetype='application/soap+xml')
            return Response(status=400)

        @app.route('/onvif/media_service', methods=['GET','POST'])
        def media_service():
            if 'wsdl' in request.args: return send_from_directory('onvif/wsdl', 'media.wsdl')
            body = request.data.decode('utf-8')
            if 'GetProfiles' in body:
                xml = f"""<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema"><SOAP-ENV:Body><trt:GetProfilesResponse><trt:Profiles token="main_profile" fixed="true"><tt:Name>MainProfile</tt:Name><tt:VideoSourceConfiguration token="video_source_config_0"><tt:Name>main_video_source</tt:Name><tt:UseCount>1</tt:UseCount><tt:SourceToken>video_source_token_0</tt:SourceToken><tt:Bounds x="0" y="0" width="{FRAME_WIDTH}" height="{FRAME_HEIGHT}"/></tt:VideoSourceConfiguration><tt:VideoEncoderConfiguration token="video_encoder_config_0"><tt:Name>main_video_encoder</tt:Name><tt:UseCount>1</tt:UseCount><tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>{FRAME_WIDTH}</tt:Width><tt:Height>{FRAME_HEIGHT}</tt:Height></tt:Resolution><tt:Quality>5</tt:Quality><tt:RateControl><tt:FrameRateLimit>{int(round(TARGET_FPS))}</tt:FrameRateLimit><tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>4096</tt:BitrateLimit></tt:RateControl><tt:SessionTimeout>PT5S</tt:SessionTimeout></tt:VideoEncoderConfiguration><tt:PTZConfiguration token="ptz_config_0"><tt:Name>main_ptz_config</tt:Name><tt:UseCount>1</tt:UseCount><tt:NodeToken>ptz_node_0</tt:NodeToken><tt:DefaultContinuousZoomVelocitySpace>http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGenericSpace</tt:DefaultContinuousZoomVelocitySpace></tt:PTZConfiguration></trt:Profiles></trt:GetProfilesResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>"""
                return Response(xml, mimetype='application/soap+xml')
            return Response(status=400)

        @app.route('/onvif/ptz_service', methods=['GET','POST'])
        def ptz_service():
            if 'wsdl' in request.args: return send_from_directory('onvif/wsdl', 'ptz.wsdl')
            body = request.data.decode('utf-8', errors='ignore')
            
            if 'GetServiceCapabilities' in body:
                xml = """<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"><SOAP-ENV:Body><tptz:GetServiceCapabilitiesResponse xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"><tptz:Capabilities MoveStatus="true"/></tptz:GetServiceCapabilitiesResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>"""
                return Response(xml, mimetype='application/soap+xml')

            if 'GetPresets' in body:
                logger.info("ONVIF: Received GetPresets request. Responding with virtual presets.")
                presets_xml = "".join([f'<tt:Preset token="{token}" xmlns:tt="http://www.onvif.org/ver10/schema"><tt:Name>{details["name"]}</tt:Name></tt:Preset>' for token, details in self.presets.items()])
                xml = f"""<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"><SOAP-ENV:Body><tptz:GetPresetsResponse xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">{presets_xml}</tptz:GetPresetsResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>"""
                return Response(xml, mimetype='application/soap+xml')

            if 'GotoPreset' in body:
                logger.info("ONVIF: Received GotoPreset request.")
                try:
                    match = re.search(r'<[a-zA-Z0-9:]*PresetToken>(\w+)</[a-zA-Z0-9:]*PresetToken>', body)
                    if match:
                        token = match.group(1)
                        if token in self.presets:
                            preset = self.presets[token]
                            logger.info(f"Triggering preset '{preset['name']}' (token: {token}, position: {preset['pos']})")
                            if self.goto_preset_thread and self.goto_preset_thread.is_alive():
                               logger.warning("A preset move is already in progress. Ignoring new request.")
                            else:
                               self.goto_preset_thread = threading.Thread(target=self._goto_preset_worker, args=(preset['name'], preset['pos']), daemon=True)
                               self.goto_preset_thread.start()
                        else:
                            logger.warning(f"Received GotoPreset request for unknown token: {token}")
                    else:
                        logger.error("Failed to parse PresetToken from GotoPreset request body.")
                except Exception as e:
                    logger.error(f"Error processing GotoPreset request: {e}")
                
                xml = """<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"><SOAP-ENV:Body><tptz:GotoPresetResponse xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"></tptz:GotoPresetResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>"""
                return Response(xml, mimetype='application/soap+xml')

            if 'GetConfigurationOptions' in body:
                xml = """<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"><SOAP-ENV:Body><tptz:GetConfigurationOptionsResponse xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"><tptz:PTZConfigurationOptions><tt:Spaces xmlns:tt="http://www.onvif.org/ver10/schema"><tt:ContinuousZoomVelocitySpace><tt:XRange><tt:Min>-1</tt:Min><tt:Max>1</tt:Max></tt:XRange></tt:ContinuousZoomVelocitySpace></tt:Spaces></tptz:PTZConfigurationOptions></SOAP-ENV:Body></SOAP-ENV:Envelope>"""
                return Response(xml, mimetype='application/soap+xml')

            if 'ContinuousMove' in body:
                logger.info("ONVIF: Received ContinuousMove request.")
                if self.zoom_thread and self.zoom_thread.is_alive():
                    self.zoom_stop_event.set(); self.zoom_thread.join()
                self.zoom_stop_event.clear()
                try:
                    match = re.search(r'<[a-zA-Z0-9:]*Zoom[^>]*?x="(-?[\d\.]*)"', body)
                    zoom_vel = float(match.group(1)) if match else 0.0
                except:
                    zoom_vel = 0.0
                action = 'tele1' if zoom_vel > 0 else 'wide1' if zoom_vel < 0 else None
                if action:
                    self.zoom_thread = threading.Thread(target=self._continuous_zoom, args=(action,), daemon=True)
                    self.zoom_thread.start()
                xml = """<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"><SOAP-ENV:Body><tptz:ContinuousMoveResponse xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"></tptz:ContinuousMoveResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>"""
                return Response(xml, mimetype='application/soap+xml')

            if 'Stop' in body:
                logger.info("ONVIF: Received Stop request.")
                if self.zoom_thread and self.zoom_thread.is_alive():
                    self.zoom_stop_event.set(); self.zoom_thread.join()
                xml = """<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"><SOAP-ENV:Body><tptz:StopResponse xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"></tptz:StopResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>"""
                return Response(xml, mimetype='application/soap+xml')
            
            logger.warning(f"ONVIF: Received unhandled PTZ request: {body[:500]}")
            return Response(status=400)

        def run_server():
            logger.info(f"ONVIF server listening on {base}")
            serve(app, host='0.0.0.0', port=ONVIF_PORT)
        self.onvif_thread = threading.Thread(target=run_server, daemon=True)
        self.onvif_thread.start()

    def _decoder_loop(self):
        logger.info("Decoder thread started")
        tmp = None
        while not self.stop_event.is_set():
            seg = self.stream.get(to=5)
            if seg is None:
                logger.error("HLS stream ended, decoder is stopping.")
                break
            data, seg_dur = seg
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".ts") as f:
                    f.write(data); tmp = f.name
                cap = cv2.VideoCapture(tmp)
                frame_count = 0
                while cap.isOpened() and not self.stop_event.is_set():
                    ret, frame = cap.read()
                    if not ret: break
                    frame_count += 1
                    try:
                        self.frame_queue.put(frame, timeout=0.5)
                    except queue.Full:
                        logger.warning("Frame queue full; skipping frame")
                cap.release()
                if frame_count and seg_dur > 0:
                    with self.status_lock:
                        self.actual_fps = self.actual_fps*0.9 + (frame_count/seg_dur)*0.1
            except Exception as e:
                logger.error(f"Decoding error: {e}")
            finally:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp); tmp = None
        logger.info("Decoder thread terminated")
        self.frame_queue.put(None)

    def _status_logger_thread(self):
        """A dedicated thread to print the status line without interrupting other logs."""
        while not self.stop_event.is_set():
            with self.status_lock:
                buffer_size = self.frame_queue.qsize()
                buffer_seconds = buffer_size / TARGET_FPS if TARGET_FPS > 0 else 0
                zoom_pos_str = f"{self.current_zoom_pos}/11" if self.current_zoom_pos != -1 else "?/11"
                zoom_info = f"Zoom: {zoom_pos_str} ({self.zoom_status})"

                status_string = (
                    f"Buffer: {buffer_size:3d}f ({buffer_seconds:4.1f}s) | "
                    f"FPS: {self.actual_fps:5.2f} | "
                    f"Proc: {self.avg_processing_time*1000:5.2f}ms | "
                    f"Sleep: {self.avg_sleep_time*1000:5.2f}ms | "
                    f"{zoom_info}"
                )

            print(f"\r{status_string:<100}", end="", flush=True)
            time.sleep(0.25)

    def start(self, novid=False):
        self.novid = novid
        if self.remote_handle == 0:
            logger.error("Camera not initialized, cannot start stream.")
            return

        logger.info("=== STARTING STREAM ===")
        self.stop_event.clear()
        self.start_onvif_server()
        threading.Thread(target=self._status_logger_thread, daemon=True).start()

        try:
            cfg = os.path.join(os.path.dirname(__file__), "mediamtx.yml")
            self.mtx_proc = subprocess.Popen(["mediamtx.exe", cfg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
            ffmpeg_cmd = [
                'ffmpeg', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{FRAME_WIDTH}x{FRAME_HEIGHT}',
                '-r', str(TARGET_FPS), '-i', '-', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                '-preset', 'veryfast', '-profile:v', 'baseline', '-tune', 'zerolatency',
                '-g', str(int(TARGET_FPS)), '-rtsp_transport', 'tcp',
                '-f', 'rtsp', f'rtsp://localhost:{RESTREAM_PORT}/{RESTREAM_PATH}'
            ]
            self.ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            logger.error(f"Failed to start streaming services: {e}")
            self._cleanup_stream_procs(); return

        self.stream = HLS(self.s, self.play)
        self.stream.start()
        self.decoder_thread = threading.Thread(target=self._decoder_loop, daemon=True)
        self.decoder_thread.start()

        min_buf = int(TARGET_FPS * 2)
        logger.info(f"Pre-buffering {min_buf} frames...")
        start_ts = time.time()
        while self.frame_queue.qsize() < min_buf:
            if self.stop_event.is_set() or not self.decoder_thread.is_alive() or time.time() - start_ts > 20:
                logger.error("Buffering failed or was aborted, stopping stream attempt.")
                self._cleanup_stream_procs(); return
            time.sleep(0.1)
        logger.info("Buffer filled, beginning playback.")

        if not self.novid:
            cv2.namedWindow("Camera Live View", cv2.WINDOW_NORMAL)

        frame_counter = 0
        playback_start_time = time.time()
        while not self.stop_event.is_set():
            if not self.decoder_thread.is_alive() and self.frame_queue.empty():
                logger.error("Decoder thread is dead and buffer is empty. Ending stream.")
                break
            
            loop_start_time = time.time()
            try:
                frame = self.frame_queue.get(timeout=5.0)
                if frame is None:
                    logger.warning("Received EOS signal from decoder.")
                    break

                if frame.shape[1] != FRAME_WIDTH or frame.shape[0] != FRAME_HEIGHT:
                    frame = cv2.resize(frame, (FRAME_WIDTH, FRAME_HEIGHT))

                if self.ffmpeg_proc and self.ffmpeg_proc.stdin:
                    self.ffmpeg_proc.stdin.write(frame.tobytes())

                if not self.novid:
                    cv2.imshow("Camera Live View", frame)
                    if cv2.waitKey(1) & 0xFF == ord('q'):
                        self.stop_event.set(); break
                
                with self.status_lock:
                    processing_time = time.time() - loop_start_time
                    self.avg_processing_time = self.avg_processing_time * 0.95 + processing_time * 0.05
                
                frame_counter += 1
                next_frame_target_time = playback_start_time + frame_counter / TARGET_FPS
                sleep_duration = next_frame_target_time - time.time()
                
                if sleep_duration > 0:
                    time.sleep(sleep_duration)
                    with self.status_lock:
                        self.avg_sleep_time = self.avg_sleep_time * 0.95 + sleep_duration * 0.05
                else:
                    with self.status_lock:
                        self.avg_sleep_time *= 0.95

            except queue.Empty:
                logger.warning("Frame buffer was empty for 5 seconds; stream has stalled.")
                break
            except Exception as e:
                logger.error(f"Error in main playback loop: {e}")
                break
        
        print()
        logger.info("Playback loop finished.")
        self._cleanup_stream_procs()

    def _cleanup_stream_procs(self):
        logger.info("Cleaning up stream-related processes (FFmpeg, MediaMTX)...")
        for proc_name in ['ffmpeg_proc', 'mtx_proc']:
            proc = getattr(self, proc_name)
            if proc and proc.poll() is None:
                try:
                    if proc.stdin: proc.stdin.close()
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception as e:
                    logger.warning(f"Could not terminate {proc_name}: {e}. Killing.")
                    proc.kill()
            setattr(self, proc_name, None)

    def stop(self):
        logger.info("=== INITIATING FULL SHUTDOWN ===")
        self.stop_event.set()
        if self.stream: self.stream.stop(); self.stream = None
        if self.decoder_thread and self.decoder_thread.is_alive(): self.decoder_thread.join(timeout=3)
        self.decoder_thread = None
        while not self.frame_queue.empty():
            try: self.frame_queue.get_nowait()
            except queue.Empty: break
        self._cleanup_stream_procs()
        try: self.s.get(f"{self.ctrl}/command?ps=2", timeout=3)
        except: pass
        if not self.novid: cv2.destroyAllWindows()
        logger.info("=== SESSION FULLY TERMINATED ===")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Camera Wi-Fi Live-View Client")
    parser.add_argument('--novid', action='store_true', help='Run without display window')
    args = parser.parse_args()
    logger.info("Starting Camera Wi-Fi Live-View Client")

    cam = Cam()
    try:
        while True:
            if not cam.connect():
                logger.error("Connection failed. Retrying in 10 seconds...")
                time.sleep(10)
                continue
            cam.start(novid=args.novid)
            logger.warning("Stream lost or stopped. Will attempt to reconnect in 10 seconds.")
            cam._cleanup_stream_procs()
            if cam.stream: cam.stream.stop()
            time.sleep(10)
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    except Exception as e:
        logger.critical(f"An unexpected error occurred: {e}", exc_info=True)
    finally:
        cam.stop()
