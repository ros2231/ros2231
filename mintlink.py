#!/usr/bin/env python3
"""MintLink PC companion.

Turns a Linux PC (built for Linux Mint / Cinnamon on X11) into a Bluetooth LE
peripheral that the MintLink web app (index.html, in Chrome on Android) connects
to. The phone becomes a touchpad, keyboard, media/volume remote, clipboard
bridge and "find my device" button.

Requires: BlueZ (bluetoothd), python3-dbus, python3-gi, xdotool, xclip (or xsel),
pactl (pulseaudio-utils). Run as your normal user (NOT with sudo), inside your
desktop session:  python3 mintlink.py

Wire protocol (matches index.html):
  Phone -> PC: writes to RX characteristic. Each write is one frame:
      byte 0 = '+' (more follows) or '=' (last frame), then up to 18 payload bytes.
  PC -> Phone: notifications on TX characteristic, same framing.
  A reassembled message is UTF-8 text: first char = command, rest = argument.
"""
import argparse
import glob
import hmac
import queue
import re
import secrets
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

BLUEZ = 'org.bluez'
SVC_UUID = '6d696e74-6c69-6e6b-0001-000000000000'
RX_UUID = '6d696e74-6c69-6e6b-0002-000000000000'   # phone writes here
TX_UUID = '6d696e74-6c69-6e6b-0003-000000000000'   # PC notifies here
CHUNK = 18
MAX_MSG = 32768
MAX_TYPE = 500
MAX_CLIP_OUT = 4000
MAX_CLIP_IN = 20000
KEY_RE = re.compile(r'^[A-Za-z0-9_+]{1,40}$')
URL_RE = re.compile(r'^https?://\S+$')
RING_SOUNDS = ['/usr/share/sounds/freedesktop/stereo/phone-incoming-call.oga',
               '/usr/share/sounds/freedesktop/stereo/bell.oga']


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


# ---------------------------------------------------------------- shell helpers
def run(cmd, **kw):
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=8, **kw)
    except Exception as e:  # missing tool, timeout, ...
        log('command failed:', cmd[0], e)


def out(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ''


def get_volume():
    try:
        v = re.search(r'(\d+)%', out(['pactl', 'get-sink-volume', '@DEFAULT_SINK@']))
        m = out(['pactl', 'get-sink-mute', '@DEFAULT_SINK@'])
        return (int(v.group(1)), 1 if 'yes' in m.lower() else 0) if v else (-1, 0)
    except Exception:
        return -1, 0


def get_battery():
    for p in glob.glob('/sys/class/power_supply/BAT*/capacity'):
        try:
            return int(open(p).read().strip())
        except Exception:
            pass
    return -1


def clip_get():
    if shutil.which('xclip'):
        return out(['xclip', '-selection', 'clipboard', '-o'])
    if shutil.which('xsel'):
        return out(['xsel', '--clipboard', '--output'])
    return ''


def clip_set(text):
    if shutil.which('xclip'):
        cmd = ['xclip', '-selection', 'clipboard']
    elif shutil.which('xsel'):
        cmd = ['xsel', '--clipboard', '--input']
    else:
        return
    run(cmd, input=text.encode())


def lock_screen():
    for cmd in (['cinnamon-screensaver-command', '-l'], ['loginctl', 'lock-session'],
                ['xdg-screensaver', 'lock']):
        if shutil.which(cmd[0]):
            run(cmd)
            return


def ring_pc():
    if shutil.which('notify-send'):
        run(['notify-send', '-u', 'critical', 'MintLink', 'Your phone is looking for you'])
    snd = next((s for s in RING_SOUNDS if os.path.exists(s)), None)
    for _ in range(6):
        if snd and shutil.which('paplay'):
            run(['paplay', snd])
        else:
            sys.stdout.write('\a')
            sys.stdout.flush()
        time.sleep(0.3)


# ------------------------------------------------------------------ BlueZ / D-Bus
class Application(dbus.service.Object):
    def __init__(self, bus):
        self.services = []
        super().__init__(bus, '/')

    def get_path(self):
        return dbus.ObjectPath('/')

    @dbus.service.method('org.freedesktop.DBus.ObjectManager', out_signature='a{oa{sa{sv}}}')
    def GetManagedObjects(self):
        r = {}
        for s in self.services:
            r[s.get_path()] = s.get_properties()
            for c in s.characteristics:
                r[c.get_path()] = c.get_properties()
        return r


class Service(dbus.service.Object):
    def __init__(self, bus, index, uuid):
        self.path = '/org/mintlink/service%d' % index
        self.uuid = uuid
        self.characteristics = []
        super().__init__(bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def get_properties(self):
        return {'org.bluez.GattService1': {
            'UUID': self.uuid, 'Primary': True,
            'Characteristics': dbus.Array([c.get_path() for c in self.characteristics], signature='o')}}

    @dbus.service.method('org.freedesktop.DBus.Properties', in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        return self.get_properties().get(interface, {})


class Characteristic(dbus.service.Object):
    def __init__(self, bus, index, uuid, flags, service):
        self.path = service.path + '/char%d' % index
        self.uuid, self.flags, self.service = uuid, flags, service
        self.notifying = False
        self.on_write = None
        super().__init__(bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def get_properties(self):
        return {'org.bluez.GattCharacteristic1': {
            'Service': self.service.get_path(), 'UUID': self.uuid,
            'Flags': dbus.Array(self.flags, signature='s')}}

    @dbus.service.method('org.freedesktop.DBus.Properties', in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        return self.get_properties().get(interface, {})

    @dbus.service.method('org.bluez.GattCharacteristic1', in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        return dbus.Array([], signature='y')

    @dbus.service.method('org.bluez.GattCharacteristic1', in_signature='aya{sv}')
    def WriteValue(self, value, options):
        if self.on_write:
            dev = str(options.get('device', 'default'))
            self.on_write(dev, bytes(bytearray(value)), int(options.get('mtu', 23)))

    @dbus.service.method('org.bluez.GattCharacteristic1')
    def StartNotify(self):
        self.notifying = True

    @dbus.service.method('org.bluez.GattCharacteristic1')
    def StopNotify(self):
        self.notifying = False

    @dbus.service.signal('org.freedesktop.DBus.Properties', signature='sa{sv}as')
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

    def send_frame(self, frame):
        if self.notifying:
            self.PropertiesChanged('org.bluez.GattCharacteristic1',
                                   {'Value': dbus.Array([dbus.Byte(b) for b in frame], signature='y')}, [])


class Advertisement(dbus.service.Object):
    def __init__(self, bus, name):
        self.path = '/org/mintlink/advertisement0'
        self.local_name = name
        super().__init__(bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def props(self):
        p = {'Type': 'peripheral', 'ServiceUUIDs': dbus.Array([SVC_UUID], signature='s')}
        if self.local_name:
            p['LocalName'] = dbus.String(self.local_name)
        return p

    @dbus.service.method('org.freedesktop.DBus.Properties', in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        return self.props() if interface == 'org.bluez.LEAdvertisement1' else {}

    @dbus.service.method('org.freedesktop.DBus.Properties', in_signature='ss', out_signature='v')
    def Get(self, interface, prop):
        return self.props()[prop]

    @dbus.service.method('org.bluez.LEAdvertisement1')
    def Release(self):
        log('advertisement released by BlueZ')


# ------------------------------------------------------------------- session logic
def mirror_problem():
    """None if a phone is ready to mirror, else a user-facing explanation."""
    if not shutil.which('scrcpy'):
        return 'Install scrcpy on the PC: sudo apt install scrcpy adb'
    if shutil.which('adb'):
        states = [p[1] for p in (l.split() for l in out(['adb', 'devices']).splitlines()) if len(p) == 2]
        if 'device' in states:
            return None
        if 'unauthorized' in states:
            return 'Unlock the phone and tap Allow on the USB debugging prompt.'
        if states:
            return 'Phone is connected but not ready. Unplug and replug the USB cable.'
        return 'No phone found. Plug it in by USB and turn on USB debugging (Settings > Developer options).'
    return None


def mirror_cmd(extra):
    return ['scrcpy', '--window-title', 'MintLink: phone screen'] + list(extra)


def mirror_foreground(extra):
    prob = mirror_problem()
    if prob:
        print(prob)
        return 1
    return subprocess.call(mirror_cmd(extra))


class ScreenShare:
    """Screen sharing. Bluetooth LE is far too slow for video, so the phone gets a
    URL + token over BLE and then watches an HTTPS MJPEG stream over the local
    Wi-Fi/LAN. ffmpeg (x11grab) only runs while someone is watching."""

    VIEWER = ('<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">'
              '<title>MintLink screen</title><style>html,body{margin:0;height:100%%;background:#000}'
              'img{width:100%%;height:100%%;object-fit:contain}</style>'
              '<img src="/stream?t=%s" onclick="document.documentElement.requestFullscreen&&'
              'document.documentElement.requestFullscreen()">')

    def __init__(self, port=8443, fps=10, width=1280):
        self.port, self.fps, self.width = port, fps, width
        self.token = secrets.token_urlsafe(16)
        self.server = None
        self.ip = None
        self.clients = []
        self.proc = None
        self.lock = threading.Lock()

    @staticmethod
    def lan_ip():
        for target in ('8.8.8.8', '10.255.255.255'):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((target, 1))
                ip = s.getsockname()[0]
                if not ip.startswith('127.'):
                    return ip
            except OSError:
                pass
            finally:
                s.close()
        raise RuntimeError('PC has no network connection. Connect it to the same Wi-Fi as your phone.')

    def ensure_cert(self, ip):
        base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
        d = os.path.join(base, 'mintlink')
        os.makedirs(d, mode=0o700, exist_ok=True)
        cert, key, stamp = (os.path.join(d, n) for n in ('cert.pem', 'key.pem', 'ip.txt'))
        have = all(os.path.exists(f) for f in (cert, key, stamp))
        if not have or open(stamp).read().strip() != ip:
            r = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                                '-keyout', key, '-out', cert, '-days', '365', '-subj', '/CN=mintlink',
                                '-addext', 'subjectAltName=IP:%s,DNS:%s' % (ip, socket.gethostname())],
                               capture_output=True, timeout=60)
            if r.returncode:
                raise RuntimeError('Could not create a certificate: ' + r.stderr.decode(errors='replace')[-150:])
            os.chmod(key, 0o600)
            open(stamp, 'w').write(ip)
        return cert, key

    def start(self):
        """Start the HTTPS server if needed. Returns (base_url, token); raises RuntimeError."""
        if not shutil.which('ffmpeg'):
            raise RuntimeError('Install ffmpeg on the PC: sudo apt install ffmpeg')
        if self.server is None:
            if not shutil.which('openssl'):
                raise RuntimeError('Install openssl on the PC: sudo apt install openssl')
            ip = self.lan_ip()
            cert, key = self.ensure_cert(ip)
            hub = self

            class Handler(BaseHTTPRequestHandler):
                timeout = 15

                def log_message(self, *a):
                    pass

                def do_GET(self):
                    u = urlparse(self.path)
                    t = parse_qs(u.query).get('t', [''])[0]
                    if not hmac.compare_digest(t.encode(), hub.token.encode()):
                        self.send_error(403)
                        return
                    if u.path == '/':
                        body = (hub.VIEWER % hub.token).encode()
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.send_header('Content-Length', str(len(body)))
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        self.wfile.write(body)
                    elif u.path == '/stream':
                        self.stream()
                    else:
                        self.send_error(404)

                def stream(self):
                    q = hub.add_client()
                    try:
                        self.send_response(200)
                        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('Cross-Origin-Resource-Policy', 'cross-origin')
                        self.end_headers()
                        while True:
                            f = q.get(timeout=15)
                            if f is None:
                                break
                            self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n'
                                             % len(f) + f + b'\r\n')
                    except Exception:
                        pass
                    finally:
                        hub.remove_client(q)

            try:
                srv = ThreadingHTTPServer(('0.0.0.0', self.port), Handler)
            except OSError as e:
                raise RuntimeError('Screen share port %d unavailable: %s' % (self.port, e.strerror or e))
            srv.daemon_threads = True
            srv.handle_error = lambda *a: None   # aborted TLS handshakes are normal
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.server, self.ip = srv, ip
            log('screen share server on https://%s:%d' % (ip, self.port))
        return 'https://%s:%d' % (self.ip, self.port), self.token

    # --- shared capture: one ffmpeg feeds every viewer
    def add_client(self):
        q = queue.Queue(maxsize=2)
        with self.lock:
            self.clients.append(q)
            if self.proc is None:
                self._start_ffmpeg()
        log('screen viewer connected')
        return q

    def remove_client(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)
            if not self.clients:
                self._stop_ffmpeg()

    def _start_ffmpeg(self):
        cmd = ['ffmpeg', '-loglevel', 'error', '-f', 'x11grab', '-framerate', str(self.fps),
               '-draw_mouse', '1', '-i', os.environ.get('DISPLAY', ':0'),
               '-vf', "scale='min(%d,iw)':-2" % self.width, '-q:v', '6',
               '-f', 'image2pipe', '-vcodec', 'mjpeg', '-']
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _stop_ffmpeg(self):
        p, self.proc = self.proc, None
        if p:
            try:
                p.terminate()
            except Exception:
                pass

    def stop(self):
        with self.lock:
            self._stop_ffmpeg()

    def _pump(self, proc):
        buf = bytearray()
        while True:
            d = proc.stdout.read(65536)
            if not d:
                break
            buf += d
            if len(buf) > 8 * 1024 * 1024:
                buf.clear()
            while True:
                e = buf.find(b'\xff\xd9')   # JPEG end marker (never appears inside the data)
                if e < 0:
                    break
                frame = bytes(buf[:e + 2])
                del buf[:e + 2]
                self._publish(frame)
        with self.lock:
            if self.proc is proc:       # ffmpeg died on its own
                self.proc = None
                for q in self.clients:
                    try:
                        q.put_nowait(None)
                    except queue.Full:
                        pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass

    def _publish(self, frame):
        with self.lock:
            cs = list(self.clients)
        for q in cs:
            try:
                q.put_nowait(frame)
            except queue.Full:          # slow viewer: drop the old frame, keep the newest
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    pass


class CameraViewer:
    """A window on the PC that shows the phone's camera (JPEG frames sent over Bluetooth)."""

    def __init__(self, on_close):
        import gi
        gi.require_version('Gtk', '3.0')
        gi.require_version('GdkPixbuf', '2.0')
        from gi.repository import Gtk, GdkPixbuf
        self.GdkPixbuf = GdkPixbuf
        self.on_close = on_close
        self.alive = True
        self.n, self.t0 = 0, time.time()
        self.win = Gtk.Window(title='MintLink: phone camera')
        self.win.set_default_size(640, 480)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)   # lets the window shrink freely
        self.img = Gtk.Image()
        scroll.add(self.img)
        self.win.add(scroll)
        self.win.connect('destroy', lambda *_: self._closed())
        self.win.show_all()

    def _closed(self):
        if self.alive:
            self.alive = False
            self.on_close()

    def close(self):
        if self.alive:
            self.alive = False
            self.win.destroy()

    def show(self, data):
        try:
            ld = self.GdkPixbuf.PixbufLoader.new_with_type('jpeg')
            ld.write(data)
            ld.close()
            pb = ld.get_pixbuf()
        except GLib.Error:
            return
        if pb is None:
            return
        a = self.win.get_allocation()
        r = min(max(a.width, 1) / pb.get_width(), max(a.height, 1) / pb.get_height())
        if abs(r - 1) > 0.01:
            pb = pb.scale_simple(max(1, int(pb.get_width() * r)), max(1, int(pb.get_height() * r)),
                                 self.GdkPixbuf.InterpType.BILINEAR)
        self.img.set_from_pixbuf(pb)
        self.n += 1
        dt = time.time() - self.t0
        if dt >= 2:
            self.win.set_title('MintLink: phone camera (%.1f fps)' % (self.n / dt))
            self.n, self.t0 = 0, time.time()


class Session:
    def __init__(self):
        self.buf = bytearray()
        self.authed = False
        self.fails = 0
        self.locked_until = 0.0
        self.mtu = 23


class MintLink:
    def __init__(self, pin, rx, tx, screen=None):
        self.pin, self.rx, self.tx = pin, rx, tx
        self.screen = screen
        self.mirror_proc = None
        self.scrcpy_args = []
        self.cam = None
        self.sessions = {}
        self.out_frames = []
        self.pumping = False
        self.work = queue.Queue()
        self.mx = self.my = 0
        self.move_pending = False
        self.mlock = threading.Lock()
        self.dragging = False
        self.host = socket.gethostname().replace(',', ' ')
        rx.on_write = self.on_write
        threading.Thread(target=self.worker, daemon=True).start()

    # --- worker thread: everything that spawns processes
    def worker(self):
        while True:
            f = self.work.get()
            try:
                f()
            except Exception as e:
                log('worker error:', e)

    def add_move(self, x, y):
        with self.mlock:
            self.mx += max(-300, min(300, x))
            self.my += max(-300, min(300, y))
            need = not self.move_pending
            self.move_pending = True
        if need:
            self.work.put(self.do_move)

    def do_move(self):
        with self.mlock:
            x, y, self.mx, self.my, self.move_pending = self.mx, self.my, 0, 0, False
        if x or y:
            run(['xdotool', 'mousemove_relative', '--', str(x), str(y)])

    # --- outgoing frames (main loop)
    def notify(self, msg):
        b = msg.encode()
        for i in range(0, max(len(b), 1), CHUNK):
            flag = 61 if i + CHUNK >= len(b) else 43
            self.out_frames.append(bytes([flag]) + b[i:i + CHUNK])
        if not self.pumping:
            self.pumping = True
            GLib.timeout_add(12, self.pump)

    def pump(self):
        if not self.out_frames:
            self.pumping = False
            return False
        self.tx.send_frame(self.out_frames.pop(0))
        return True

    def reply_later(self, msg):
        GLib.idle_add(lambda: (self.notify(msg), False)[1])

    # --- incoming frames (main loop)
    def on_write(self, dev, value, mtu=23):
        if not value:
            return
        s = self.sessions.setdefault(dev, Session())
        s.mtu = mtu
        flag = value[0]
        if flag not in (43, 61):
            return
        s.buf += value[1:]
        if len(s.buf) > MAX_MSG:
            s.buf.clear()
            return
        if flag == 61:
            raw = bytes(s.buf)
            s.buf.clear()
            if raw[:1] == b'\x01':          # binary message (camera frame)
                self.handle_binary(s, raw[1:])
            else:
                self.handle(s, raw.decode('utf-8', 'replace'))

    def drop(self, dev):
        if self.sessions.pop(dev, None) is not None:
            log('phone disconnected')
            self.release_drag()
            self.camera('0')

    def release_drag(self):
        if self.dragging:
            self.dragging = False
            self.work.put(lambda: run(['xdotool', 'mouseup', '1']))

    def any_authed(self):
        return any(s.authed for s in self.sessions.values())

    def auth(self, s, pin):
        now = time.time()
        if now < s.locked_until:
            self.notify('E')
            return
        if hmac.compare_digest(pin.encode(), self.pin.encode()):
            s.authed, s.fails = True, 0
            log('phone authenticated')
            self.notify('A%d' % max(23, min(517, s.mtu)))   # tells the app how big its writes can be
        else:
            s.fails += 1
            log('wrong PIN attempt', s.fails)
            if s.fails >= 5:
                s.locked_until, s.fails = now + 30, 0
                log('too many attempts, locked for 30s')
            self.notify('E')

    def handle(self, s, m):
        if not m:
            return
        c, a = m[0], m[1:]
        if c == 'a':
            return self.auth(s, a.strip())
        if not s.authed:
            return
        Q = self.work.put
        if c == 'm':
            try:
                x, y = (int(n) for n in a.split(','))
            except ValueError:
                return
            self.add_move(x, y)
        elif c == 'c':
            Q(lambda: run(['xdotool', 'click', '3' if a == 'r' else '1']))
        elif c == 's':
            Q(lambda: run(['xdotool', 'click', '4' if a.startswith('-') else '5']))
        elif c == 'd':
            self.dragging = a == '1'
            Q(lambda: run(['xdotool', 'mousedown' if a == '1' else 'mouseup', '1']))
        elif c == 'k':
            keys = a.split()[:40]
            if keys and all(KEY_RE.match(k) for k in keys):
                Q(lambda: run(['xdotool', 'key', '--clearmodifiers', '--'] + keys))
        elif c == 't':
            text = a[:MAX_TYPE]
            if text:
                Q(lambda: run(['xdotool', 'type', '--delay', '8', '--', text]))
        elif c == 'v':
            if a == '+':
                arg = '+5%'
            elif a == '-':
                arg = '-5%'
            elif a == 'm':
                Q(lambda: run(['pactl', 'set-sink-mute', '@DEFAULT_SINK@', 'toggle']))
                return
            elif a.isdigit():
                arg = '%d%%' % min(100, int(a))
            else:
                return
            Q(lambda: run(['pactl', 'set-sink-volume', '@DEFAULT_SINK@', arg]))
        elif c == '?':
            Q(self.send_status)
        elif c == 'g':
            Q(lambda: self.reply_later('C' + clip_get()[:MAX_CLIP_OUT]))
        elif c == 'x':
            Q(lambda: clip_set(a[:MAX_CLIP_IN]))
        elif c == 'o':
            if URL_RE.match(a.strip()):
                url = a.strip()
                subprocess.Popen(['xdg-open', url], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        elif c == 'r':
            threading.Thread(target=ring_pc, daemon=True).start()
        elif c == 'u':
            self.camera(a)
        elif c == 'h':
            if a == '1' and self.screen:
                Q(self.screen_start)
        elif c == 'p':
            Q(lambda: self.reply_later('I' + self.mirror(a)))
        elif c == 'l':
            Q(lock_screen)
        elif c == 'z':
            Q(lambda: run(['systemctl', 'suspend']))

    def camera(self, a):
        """Phone camera -> PC window. 'F' tells the phone it may send the next frame."""
        if a == '0':
            cam, self.cam = self.cam, None
            if cam:
                cam.close()
            return
        if self.cam:
            self.notify('F')
            return
        try:
            self.cam = CameraViewer(self.cam_closed)
        except Exception as e:
            log('camera window failed:', e)
            self.notify('JCould not open a window on the PC. Try: sudo apt install gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0')
            self.notify('K')
            return
        log('camera window opened')
        self.notify('F')

    def cam_closed(self):
        if self.cam:
            self.cam = None
            self.notify('JCamera window closed on the PC.')
            self.notify('K')

    def handle_binary(self, s, data):
        if not s.authed or not self.cam:
            return
        if data[:2] != b'\xff\xd8' or data[-2:] != b'\xff\xd9':   # not a complete JPEG: no ack, phone retries
            return
        self.cam.show(data)
        self.notify('F')

    def mirror(self, a):
        """Show the phone's screen on the PC with scrcpy (USB, no network needed)."""
        p = self.mirror_proc
        running = p is not None and p.poll() is None
        if a == '0':
            if running:
                p.terminate()
                return 'Stopped mirroring.'
            return 'Not mirroring.'
        if running:
            return 'Already mirroring. Look for the scrcpy window on the PC.'
        prob = mirror_problem()
        if prob:
            return prob
        try:
            self.mirror_proc = subprocess.Popen(mirror_cmd(self.scrcpy_args), stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            return 'Could not start scrcpy: %s' % e
        time.sleep(1.5)
        if self.mirror_proc.poll() is not None:
            return 'scrcpy exited. Run  python3 mintlink.py --mirror  in a terminal to see why.'
        return 'Phone screen opened on the PC.'

    def screen_start(self):
        try:
            base, tok = self.screen.start()
            self.reply_later('H%s|%s' % (base, tok))
        except RuntimeError as e:
            log('screen share:', e)
            self.reply_later('X' + str(e))
        except Exception as e:
            log('screen share failed:', e)
            self.reply_later('XScreen share failed: %s' % e)

    def send_status(self):
        v, mu = get_volume()
        self.reply_later('S%d,%d,%d,%s' % (v, mu, get_battery(), self.host))

    def ring_phone(self):
        if self.any_authed():
            self.notify('R')
            log('ringing phone')
        else:
            log('no phone connected')


# --------------------------------------------------------------------------- main
def find_adapter(bus):
    om = dbus.Interface(bus.get_object(BLUEZ, '/'), 'org.freedesktop.DBus.ObjectManager')
    for path, ifaces in om.GetManagedObjects().items():
        if 'org.bluez.GattManager1' in ifaces and 'org.bluez.LEAdvertisingManager1' in ifaces:
            return path
    return None


def ensure_powered(props):
    """Make sure the Bluetooth adapter is on, with a useful message if it can't be."""
    def powered():
        return bool(props.Get('org.bluez.Adapter1', 'Powered'))
    if powered():
        return
    for attempt in range(2):
        try:
            props.Set('org.bluez.Adapter1', 'Powered', dbus.Boolean(True))
            time.sleep(0.5)
            if powered():
                return
        except dbus.exceptions.DBusException as e:
            log('could not power on the adapter:', e.get_dbus_message() or e)
        if attempt == 0 and shutil.which('rfkill'):
            log('trying: rfkill unblock bluetooth')
            run(['rfkill', 'unblock', 'bluetooth'])
            time.sleep(1)
    sys.exit('\nBluetooth is off and could not be turned on.\n'
             '  1. Check:  rfkill list bluetooth   (look for "Soft blocked: yes" or "Hard blocked: yes")\n'
             '  2. Fix:    sudo rfkill unblock bluetooth     (a hard block means a physical switch / Fn key)\n'
             '  3. Then:   bluetoothctl power on\n'
             '  4. Still failing? sudo systemctl restart bluetooth   and check:  systemctl status bluetooth\n'
             'Then run MintLink again.')


def main():
    ap = argparse.ArgumentParser(description='MintLink PC companion')
    ap.add_argument('--pin', help='fixed PIN (4-8 digits); default is a random 6-digit PIN')
    ap.add_argument('--name', default='MintLink', help='advertised Bluetooth name')
    ap.add_argument('--mirror', action='store_true',
                    help="just show the phone's screen on this PC (USB + scrcpy) and exit; no Bluetooth needed")
    ap.add_argument('--scrcpy-args', default='',
                    help="extra scrcpy options, e.g. '--no-control --max-size 1280'")
    ap.add_argument('--screen-port', type=int, default=8443, help='HTTPS port for screen share')
    ap.add_argument('--screen-fps', type=int, default=10, help='screen share frames per second')
    ap.add_argument('--screen-width', type=int, default=1280, help='max screen share width in pixels')
    args = ap.parse_args()
    if args.mirror:
        if not os.environ.get('DISPLAY'):
            sys.exit('No DISPLAY. Run this inside your desktop session.')
        sys.exit(mirror_foreground(shlex.split(args.scrcpy_args)))
    if args.pin and not re.fullmatch(r'\d{4,8}', args.pin):
        sys.exit('PIN must be 4-8 digits.')
    pin = args.pin or '%06d' % secrets.randbelow(1000000)

    for tool in ('xdotool',):
        if not shutil.which(tool):
            sys.exit('Missing %s. Install with: sudo apt install xdotool xclip pulseaudio-utils' % tool)
    if not os.environ.get('DISPLAY'):
        sys.exit('No DISPLAY. Run this inside your desktop session (not over plain SSH, not with sudo).')

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    adapter = find_adapter(bus)
    if not adapter:
        sys.exit('No Bluetooth LE adapter found. Is bluetoothd running and Bluetooth enabled?')
    props = dbus.Interface(bus.get_object(BLUEZ, adapter), 'org.freedesktop.DBus.Properties')
    ensure_powered(props)

    app = Application(bus)
    svc = Service(bus, 0, SVC_UUID)
    rx = Characteristic(bus, 0, RX_UUID, ['write', 'write-without-response'], svc)
    tx = Characteristic(bus, 1, TX_UUID, ['notify'], svc)
    svc.characteristics += [rx, tx]
    app.services.append(svc)
    link = MintLink(pin, rx, tx, ScreenShare(args.screen_port, args.screen_fps, args.screen_width))
    link.scrcpy_args = shlex.split(args.scrcpy_args)

    def on_props(iface, changed, invalidated, path=None):
        if iface == 'org.bluez.Device1' and 'Connected' in changed and not changed['Connected']:
            link.drop('%s' % path)
    bus.add_signal_receiver(on_props, dbus_interface='org.freedesktop.DBus.Properties',
                            signal_name='PropertiesChanged', path_keyword='path')

    loop = GLib.MainLoop()
    gatt_mgr = dbus.Interface(bus.get_object(BLUEZ, adapter), 'org.bluez.GattManager1')
    ad_mgr = dbus.Interface(bus.get_object(BLUEZ, adapter), 'org.bluez.LEAdvertisingManager1')
    ad = Advertisement(bus, args.name)

    def fatal(what):
        def h(e):
            log('FAILED to %s: %s' % (what, e))
            loop.quit()
        return h

    def ad_ok():
        log('advertising as "%s"' % (ad.local_name or 'unnamed device'))

    def ad_err(e):
        # A 128-bit UUID plus the name can overflow the 31-byte advert; retry unnamed.
        if ad.local_name:
            log('advertising with a name failed (%s); retrying without it' % e)
            ad.local_name = None
            ad_mgr.RegisterAdvertisement(ad.get_path(), {}, reply_handler=ad_ok,
                                         error_handler=fatal('start advertising'))
        else:
            fatal('start advertising')(e)

    def gatt_ok():
        ad_mgr.RegisterAdvertisement(ad.get_path(), {}, reply_handler=ad_ok, error_handler=ad_err)

    gatt_mgr.RegisterApplication(app.get_path(), {}, reply_handler=gatt_ok,
                                 error_handler=fatal('register GATT service'))

    print('\n  MintLink is running.\n  PIN: %s\n' % pin)
    print('  Open the web app on your phone (Chrome), tap Connect, pick the device, enter the PIN.')
    if sys.stdin.isatty():
        print('  Type  r + Enter  to ring the phone,  p + Enter  to show the phone screen,  q + Enter  to quit.\n')

        def on_stdin(fd, cond):
            line = sys.stdin.readline()
            if not line:
                return False
            cmd = line.strip().lower()
            if cmd == 'r':
                link.ring_phone()
            elif cmd == 'p':
                link.work.put(lambda: log(link.mirror('1')))
            elif cmd == 'q':
                loop.quit()
            return True
        GLib.io_add_watch(sys.stdin, GLib.IO_IN, on_stdin)

    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        link.release_drag()
        link.camera('0')
        link.screen.stop()
        if link.mirror_proc and link.mirror_proc.poll() is None:
            link.mirror_proc.terminate()
        time.sleep(0.2)
        try:
            ad_mgr.UnregisterAdvertisement(ad.get_path())
        except Exception:
            pass


if __name__ == '__main__':
    main()
