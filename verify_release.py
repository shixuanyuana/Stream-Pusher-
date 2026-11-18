import pathlib, subprocess, socket, time
root=pathlib.Path(__file__).resolve().parent
release=root/'RTSP推流工具_1.7.0_clip'
with socket.socket() as s:
    s.bind(('127.0.0.1',0)); port=s.getsockname()[1]
with open(root/'verify_release.log','w',encoding='utf-8') as log:
    p=subprocess.Popen([str(release/'rtsp_pusher.exe'),'--videos',r'D:\download\2026-11-14-15-58-52_2026-11-14-16-24-38.mp4','--copy','--no-loop','--start','1:00','--end','1:12','--port',str(port)],stdout=log,stderr=log)
    try:
        time.sleep(8)
        # The CLI may round an odd requested port upwards.
        import re
        text=(root/'verify_release.log').read_text(encoding='utf-8')
        actual=re.search(r'-> rtsp://[^:]+:(\d+)/video1',text)
        assert actual, text
        result=subprocess.run([str(release/'ffmpeg/ffprobe.exe'),'-v','error','-rtsp_transport','tcp','-show_entries','stream=codec_name,width,height,avg_frame_rate','-of','json',f'rtsp://127.0.0.1:{actual[1]}/video1'],capture_output=True,text=True,timeout=20)
        print(result.stdout,result.stderr)
        assert result.returncode==0
        assert 'hevc' in result.stdout and '5984' in result.stdout and '50/1' in result.stdout
    finally:
        subprocess.run(['taskkill','/F','/T','/PID',str(p.pid)],capture_output=True)
        p.wait(timeout=10)
subprocess.run([str(release/'RTSP推流工具.exe'),'--selftest'],check=True,timeout=20)
print((release/'selftest_result.txt').read_text(encoding='utf-8'))
