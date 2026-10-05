import socket
import ssl
import threading
import traceback

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
CERT = str(ROOT / "certs" / "livekit.pem")
KEY = str(ROOT / "certs" / "livekit-key.pem")
LISTEN_HOST, LISTEN_PORT = "0.0.0.0", 7443
TARGET = ("127.0.0.1", 7880)


def pump(src, dst, label):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        # Reset/broken-pipe on call teardown is normal; keep the cause visible so a
        # genuine proxy fault is not mistaken for a healthy connection.
        print(f"[wss] {label} pump ended: {traceback.format_exc(limit=1).strip()}", flush=True)
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except Exception:
            pass


def handle(client, addr):
    tls = None
    up = None
    try:
        print(f"[wss] TCP accept from {addr[0]}:{addr[1]}", flush=True)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT, KEY)
        tls = ctx.wrap_socket(client, server_side=True)
        print(f"[wss] TLS ok for {addr[0]}, connecting backend", flush=True)
        up = socket.create_connection(TARGET, timeout=10)
        up.settimeout(None)  # Connection timeout must not become an idle-call timeout.
        print(f"[wss] proxying {addr[0]} -> {TARGET[0]}:{TARGET[1]}", flush=True)
    except Exception:
        print(f"[wss] handshake failed for {addr[0]}: {traceback.format_exc(limit=1).strip()}", flush=True)
        for s in (client, tls):
            try:
                if s:
                    s.close()
            except Exception:
                pass
        return
    t1 = threading.Thread(target=pump, args=(tls, up, "c2s"), daemon=True)
    t2 = threading.Thread(target=pump, args=(up, tls, "s2c"), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    print(f"[wss] closed {addr[0]}", flush=True)
    for s in (tls, up):
        try:
            s.close()
        except Exception:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(128)
    print(
        f"wss-proxy listening {LISTEN_HOST}:{LISTEN_PORT} -> {TARGET[0]}:{TARGET[1]}",
        flush=True,
    )
    while True:
        c, addr = srv.accept()
        threading.Thread(target=handle, args=(c, addr), daemon=True).start()


if __name__ == "__main__":
    main()