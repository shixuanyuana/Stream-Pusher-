"""Local RTSP publisher bridge: normalize equivalent HEVC SDP parameters only.

RTP payload bytes are forwarded unchanged. Distinct parameter sets are rejected
instead of silently choosing one and changing the meaning of the stream.
"""
import base64
import re
import socket
import threading


def normalize_sdp(body):
    def replace(match):
        values = match[2].split(b',')
        decoded = [base64.b64decode(v, validate=True).rstrip(b'\0') for v in values]
        if len(set(decoded)) != 1:
            raise ValueError('HEVC SDP contains distinct parameter sets; cannot safely normalize')
        return match[1] + values[0]
    return re.sub(rb'(sprop-(?:vps|sps|pps)=)([^;\s]+)', replace, body)


class PublisherBridge:
    def __init__(self, target_port):
        self.target_port = target_port
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self.forward, args=(client,), daemon=True).start()

    def forward(self, client):
        upstream = socket.socket()
        def downstream():
            try:
                while data := upstream.recv(65536):
                    client.sendall(data)
            except OSError:
                pass
            finally:
                client.close()
        try:
            upstream.connect(('127.0.0.1', self.target_port))
            threading.Thread(target=downstream, daemon=True).start()
            with client.makefile('rb') as stream:
                while first := stream.read(1):
                    if first == b'$':
                        header = stream.read(3)
                        if len(header) != 3:
                            break
                        data = stream.read(int.from_bytes(header[1:], 'big'))
                        upstream.sendall(first + header + data)
                        continue
                    header = first + stream.readline()
                    while (line := stream.readline()) not in (b'\r\n', b''):
                        header += line
                    header += b'\r\n'
                    length = re.search(rb'Content-Length:\s*(\d+)', header, re.I)
                    body = stream.read(int(length[1])) if length else b''
                    if header.startswith(b'ANNOUNCE '):
                        body = normalize_sdp(body)
                        header = re.sub(rb'(Content-Length:\s*)\d+', lambda m: m[1] + str(len(body)).encode(), header, flags=re.I)
                    upstream.sendall(header + body)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass
        except (OSError, ValueError) as exc:
            print(f'RTSP SDP bridge: {exc}', flush=True)
        finally:
            upstream.close()
            client.close()

    def close(self):
        self.listener.close()
