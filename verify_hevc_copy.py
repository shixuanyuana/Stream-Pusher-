import subprocess, pathlib, time
import socket
from rtsp_sdp_proxy import PublisherBridge
root = pathlib.Path(__file__).resolve().parent
ff = str(root / 'RTSP推流工具/ffmpeg/ffmpeg.exe')
probe = str(root / 'RTSP推流工具/ffmpeg/ffprobe.exe')
src = r'D:\download\2026-11-14-15-58-52_2026-11-14-16-24-38.mp4'
cfg = root / 'verify_hevc.yml'
with socket.socket() as available:
    available.bind(('127.0.0.1',0))
    port=available.getsockname()[1]
cfg.write_text(f'rtspAddress: 127.0.0.1:{port}\nrtspTransports: [tcp]\nrtmp: no\nhls: no\nwebrtc: no\nsrt: no\npaths:\n  test:\n', encoding='utf-8')
children=[]
try:
    with open(root/'verify_hevc_server.log','w') as log:
        server=subprocess.Popen([str(root/'mediamtx/mediamtx.exe'),str(cfg)],stdout=log,stderr=log); children.append(server)
        time.sleep(1)
        with open(root/'verify_hevc_push.log','w') as pushlog:
            bridge=PublisherBridge(port)
            dest=subprocess.Popen([ff,'-v','error','-re','-i',src,'-c:v','copy','-an','-f','rtsp','-rtsp_transport','tcp',f'rtsp://127.0.0.1:{bridge.port}/test'],stdout=pushlog,stderr=pushlog);children.append(dest)
            time.sleep(5)
            result=subprocess.run([probe,'-v','error','-rtsp_transport','tcp','-show_entries','stream=codec_name,width,height,avg_frame_rate','-of','json',f'rtsp://127.0.0.1:{port}/test'],capture_output=True,text=True,timeout=20)
            print(result.returncode,result.stdout,result.stderr)
finally:
    for p in reversed(children):
        if p.poll() is None: p.kill()
        p.wait()
