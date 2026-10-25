"""Минимальный WebSocket-клиент (только стандартная библиотека): текстовые кадры, ping/pong, close. Для OpenAI Realtime."""
import base64, os, socket, ssl, struct, threading
from urllib.parse import urlparse


class WS:
    def __init__(self, url, headers=(), timeout=20):
        u = urlparse(url); port = u.port or (443 if u.scheme == "wss" else 80)
        s = socket.create_connection((u.hostname, port), timeout)
        if u.scheme == "wss": s = ssl.create_default_context().wrap_socket(s, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + ("?" + u.query if u.query else "")
        req = [f"GET {path} HTTP/1.1", f"Host: {u.hostname}", "Upgrade: websocket", "Connection: Upgrade",
               f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"] + list(headers)
        s.sendall(("\r\n".join(req) + "\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = s.recv(4096)
            if not c: raise OSError("соединение закрыто при рукопожатии")
            buf += c
        head, self.rest = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]: raise OSError("рукопожатие отклонено: " + head.split(b"\r\n")[0].decode(errors="replace"))
        self.s, self.lock, self.closed = s, threading.Lock(), False

    def _read(self, n):
        while len(self.rest) < n:
            c = self.s.recv(65536)
            if not c: raise OSError("соединение закрыто")
            self.rest += c
        d, self.rest = self.rest[:n], self.rest[n:]
        return d

    def send(self, text):
        data = text.encode() if isinstance(text, str) else text
        n = len(data); hdr = bytes([0x81])
        hdr += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data)) if n < 2000 else _xor(data, mask)
        with self.lock: self.s.sendall(hdr + mask + body)

    def recv(self):
        """Следующее текстовое сообщение (ping/pong обрабатываем сами); при закрытии — OSError."""
        msg, op0 = b"", None
        while True:
            h = self._read(2); fin, op, ln = h[0] & 0x80, h[0] & 0x0F, h[1] & 0x7F
            if ln == 126: ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127: ln = struct.unpack(">Q", self._read(8))[0]
            data = self._read(ln)
            if op == 0x9:
                with self.lock: self.s.sendall(bytes([0x8A, 0x80]) + b"\0\0\0\0")  # pong без полезной нагрузки
                continue
            if op == 0xA: continue
            if op == 0x8: self.closed = True; raise OSError("сервер закрыл соединение")
            if op0 is None: op0 = op
            msg += data
            if fin: return msg.decode("utf-8", "replace")

    def close(self):
        self.closed = True
        try: self.s.close()
        except OSError: pass


def _xor(data, mask):
    """Маска для больших кадров (звук): через целые числа, на порядки быстрее побайтового цикла."""
    n = len(data); m = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")
