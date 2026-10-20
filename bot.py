import glob
import html
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
HERE = os.path.dirname(os.path.abspath(__file__))
_real_getaddrinfo = socket.getaddrinfo
_dns_cache = {}

def _cached_getaddrinfo(*args, **kwargs):
    key = (args, tuple(sorted(kwargs.items())))
    hit = _dns_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    res = _real_getaddrinfo(*args, **kwargs)
    res.sort(key=lambda ai: ai[0] != socket.AF_INET)
    _dns_cache[key] = (time.time(), res)
    return res
socket.getaddrinfo = _cached_getaddrinfo

def load_env(path):
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and (not line.startswith('#')) and ('=' in line):
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
load_env(os.path.join(HERE, 'config.env'))
TOKEN = os.environ['BOT_TOKEN']
ALLOWED = {int(x) for x in os.environ.get('ALLOWED_USERS', '').replace(',', ' ').split()}
MIRROR_URL = os.environ.get('MIRROR_URL', '')
SFTP_HOST = os.environ.get('SFTP_HOST', '')
SFTP_PORT = os.environ.get('SFTP_PORT', '22')
SFTP_USER = os.environ.get('SFTP_USER', 'bot-uploader')
SFTP_KEY = os.path.expanduser(os.environ.get('SFTP_KEY', os.path.join(HERE, '.ssh', 'id_ed25519')))
SFTP_KNOWN_HOSTS = os.path.expanduser(os.environ.get('SFTP_KNOWN_HOSTS', os.path.join(HERE, '.ssh', 'known_hosts')))
SFTP_REMOTE_DIR = os.environ.get('SFTP_REMOTE_DIR', '/files')
EDIT_EVERY = float(os.environ.get('EDIT_INTERVAL', '10'))
LOG_DIR = os.path.join(HERE, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
BUILDS_DIR = os.path.join(HERE, 'builds')
os.makedirs(BUILDS_DIR, exist_ok=True)
API = f'https://api.telegram.org/bot{TOKEN}/'
_tls = threading.local()

def last_tg_error():
    return getattr(_tls, 'err', '')

def tg(method, timeout=30, files=None, **params):
    _tls.err = ''
    params = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items() if v is not None}
    if files:
        boundary = uuid.uuid4().hex
        body = b''
        for k, v in params.items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, (name, data) in files.items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + data + b'\r\n'
        body += f'--{boundary}--\r\n'.encode()
        req = urllib.request.Request(API + method, data=body, headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    else:
        req = urllib.request.Request(API + method, data=urllib.parse.urlencode(params).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r).get('result')
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors='replace')
        _tls.err = err
        if 'message is not modified' not in err:
            print(f'[tg] {method} failed: {e.code} {err}')
        if e.code == 429:
            try:
                time.sleep(json.loads(err)['parameters']['retry_after'])
            except Exception:
                time.sleep(5)
    except Exception as e:
        print(f'[tg] {method} error: {e}')
    return None

def send(chat, text, reply_to=None, thread=None, reply_markup=None):
    res = tg('sendMessage', chat_id=chat, text=text, parse_mode='HTML', message_thread_id=thread, disable_web_page_preview='true', reply_to_message_id=reply_to, reply_markup=reply_markup)
    if res is None and reply_to:
        res = tg('sendMessage', chat_id=chat, text=text, parse_mode='HTML', message_thread_id=thread, disable_web_page_preview='true', reply_markup=reply_markup)
    return res

def edit(chat, msg_id, text, reply_markup=None):
    return tg('editMessageText', chat_id=chat, message_id=msg_id, text=text, parse_mode='HTML', disable_web_page_preview='true', reply_markup=reply_markup)

def answer_cb(cb_id, text=None, alert=False):
    tg('answerCallbackQuery', callback_query_id=cb_id, text=text, show_alert='true' if alert else None)

def send_file(chat, path, caption=None, reply_to=None, thread=None):
    with open(path, 'rb') as f:
        data = f.read()
    return tg('sendDocument', timeout=120, chat_id=chat, caption=caption, reply_to_message_id=reply_to, message_thread_id=thread, files={'document': (os.path.basename(path), data)})

def load_devices():
    with open(os.path.join(HERE, 'devices.json')) as f:
        return json.load(f)

def fmt_time(s):
    s = int(s)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f'{h}h {m:02d}m {s:02d}s' if h else f'{m}m {s:02d}s'

def fmt_size(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.1f} {unit}' if unit != 'B' else f'{n} B'
        n /= 1024

def bar(pct, width=20):
    filled = min(width, int(width * pct / 100))
    return '█' * filled + '░' * (width - filled)

def fmt_num(n):
    return str(n)
_cpu_prev = None

def resources(path=None):
    global _cpu_prev
    parts = []
    try:
        with open('/proc/stat') as f:
            vals = [int(x) for x in f.readline().split()[1:]]
        idle, total = (vals[3] + vals[4], sum(vals))
        if _cpu_prev and total > _cpu_prev[1]:
            busy = 1 - (idle - _cpu_prev[0]) / (total - _cpu_prev[1])
            parts.append(('CPU', f'{busy * 100:.0f}%'))
        _cpu_prev = (idle, total)
    except (OSError, ValueError, IndexError):
        pass
    try:
        mem = {}
        with open('/proc/meminfo') as f:
            for line in f:
                k, v = line.split(':', 1)
                mem[k] = int(v.split()[0]) * 1024
        used = mem['MemTotal'] - mem['MemAvailable']
        parts.append(('RAM', f"{fmt_size(used)} / {fmt_size(mem['MemTotal'])}"))
    except (OSError, ValueError, KeyError):
        pass
    try:
        parts.append(('Load', f'{os.getloadavg()[0]:.1f} ({os.cpu_count()} cores)'))
    except OSError:
        pass
    if path:
        try:
            st = os.statvfs(path)
            parts.append(('Disk free', fmt_size(st.f_bavail * st.f_frsize)))
        except OSError:
            pass
    return parts

def phase_for(desc):
    d = desc.lower()
    if 'bootstrap' in d or 'soong_build' in d or 'analyzing android.bp' in d or ('blueprint' in d):
        return 'Soong analysis'
    if d.startswith(('including ', 'finishing make ', 'writing make ', 'initializing make ')) or 'kati' in d or 'legacy make' in d:
        return 'Reading makefiles'
    return 'Compiling'
PROGRESS_RE = re.compile('^\\[\\s*(?:\\d+% )?(\\d+)/(\\d+)(?: ([^\\]]*?) remaining)?\\]\\s*(.*)')
ANSI_RE = re.compile('\\x1b\\[[0-9;?]*[A-Za-z]')

class Job:

    def __init__(self, chat, reply_to, rom, device, cfg, user, thread=None):
        self.chat, self.reply_to, self.thread = (chat, reply_to, thread)
        self.rom, self.device, self.cfg, self.user = (rom, device, cfg, user)
        self.stage = 'Starting'
        self.pct = 0.0
        self.phase = 'Setting up (lunch)'
        self.setup_step = ''
        self.done_n = self.total_n = 0
        self.eta = ''
        self.action = ''
        self.started = time.time()
        self.finished = None
        self.proc = None
        self.cancelled = False
        self.result = None
        self.zips = []
        self.sizes = {}
        self.links = {}
        self.store_dir = None
        self.uploading = None
        self.missing = []
        self.variants = cfg.get('variants', [])
        self.variant = None
        self.variant_re = re.compile(cfg['variant_re']) if cfg.get('variant_re') else None
        self.up_pct = 0
        self.up_speed = 0
        self.error = None
        self.msg_id = None
        stamp = time.strftime('%Y%m%d-%H%M%S')
        self.log_path = os.path.join(LOG_DIR, f'{rom}_{device}_{stamp}.log')
        self._last_text = None
        self._last_edit = 0
        self._edit_lock = threading.Lock()

    def render(self):
        elapsed = (self.finished or time.time()) - self.started
        building = self.result is None and self.stage == 'Building'
        lines = [f'<b>{html.escape(self.rom.upper())} | {html.escape(self.device)}</b>', f'<b>Stage:</b> {html.escape(self.stage)}' + (f' - {html.escape(self.phase)}' if building else ''), f'<b>Elapsed:</b> {fmt_time(elapsed)}']
        if self.variant and building:
            pos = f' ({self.variants.index(self.variant) + 1}/{len(self.variants)})' if self.variant in self.variants else ''
            lines.append(f'<b>Variant:</b> {html.escape(self.variant)}{pos}')
        if building and self.phase != 'Compiling' and self.setup_step:
            lines.append(f'<b>Preparing:</b> step {self.setup_step}')
        if self.total_n and building:
            shown = min(self.pct, 99.9)
            lines.append('')
            lines.append('<b>Build progress:</b>')
            lines.append(f'<code>[{bar(shown)}] {shown:5.1f}%</code>')
            lines.append(f'<b>Actions:</b> {fmt_num(self.done_n)} / {fmt_num(self.total_n)}')
            if self.eta:
                lines.append(f'<b>ETA:</b> {html.escape(self.eta)}')
        elif self.total_n:
            lines.append(f'<b>Build:</b> {self.pct:.1f}% ({fmt_num(self.done_n)} / {fmt_num(self.total_n)} actions)')
        if self.action and building:
            lines.append('')
            lines.append('<b>Running now:</b>')
            lines.append(f'<code>{html.escape(self.action[:160])}</code>')
        for z in self.zips:
            sz = self.sizes.get(z) or (os.path.getsize(z) if os.path.exists(z) else None)
            size = fmt_size(sz) if sz is not None else '?'
            lines.append('')
            lines.append(f'<b>File:</b> <code>{html.escape(os.path.basename(z))}</code> ({size})')
            if z in self.links:
                lines.append(f'<b>Download:</b> {html.escape(self.links[z])}')
            elif z == self.uploading:
                lines.append(f'<code>[{bar(self.up_pct)}] {self.up_pct:5.1f}%</code>  {fmt_size(self.up_speed)}/s')
        for v in self.missing:
            lines.append(f'<b>{html.escape(v)}:</b> not built')
        if building:
            res = resources(os.path.expanduser(self.cfg.get('cwd', '~/y')))
            if res:
                lines.append('')
                lines.append('<b>Resources:</b>')
                lines += [f'{k}: <code>{html.escape(v)}</code>' for k, v in res]
        if self.error:
            lines.append(f'\n<pre>{html.escape(self.error[-1500:])}</pre>')
        lines.append(f'\n<i>by {html.escape(self.user)}</i>')
        return '\n'.join(lines)

    def update(self, force=False):
        with self._edit_lock:
            self._update(force)

    def _update(self, force):
        if not force and time.time() - self._last_edit < EDIT_EVERY:
            return
        text = self.render()
        if text != self._last_text:
            if self.msg_id is None or (edit(self.chat, self.msg_id, text) is None and 'message to edit not found' in last_tg_error()):
                msg = send(self.chat, text, thread=self.thread)
                self.msg_id = msg['message_id'] if msg else None
            self._last_text = text
        self._last_edit = time.time()

    def run(self):
        try:
            self._run()
        except BaseException as e:
            print(f'[job] crashed: {e!r}')
            if self.proc and self.proc.poll() is None:
                os.killpg(self.proc.pid, signal.SIGINT)
        finally:
            if self.result is None:
                self.result, self.stage = ('fail', 'Bot error')
                self.finished = self.finished or time.time()

    def _run(self):
        msg = send(self.chat, self.render(), reply_to=self.reply_to, thread=self.thread)
        self.msg_id = msg['message_id'] if msg else None
        try:
            ok = self.build()
            if ok:
                self.upload()
        except Exception as e:
            self.result, self.stage, self.error = ('fail', 'Bot error', repr(e))
        self.finished = self.finished or time.time()
        if self.result is None:
            self.result = 'ok'
            self.stage = 'Done'
        self.update(force=True)
        links = ''.join((f'\n<code>{html.escape(os.path.basename(z))}</code>\n{html.escape(u)}' for z, u in self.links.items()))
        rd = f'{html.escape(self.rom)} {html.escape(self.device)}'
        summary = {'ok': f'{rd} done in {fmt_time(self.finished - self.started)}' + links, 'fail': f'{rd} failed ({html.escape(self.stage)})' + links, 'cancelled': f'{rd} cancelled'}[self.result]
        send(self.chat, summary, reply_to=self.msg_id, thread=self.thread)
        if self.result == 'fail' and os.path.exists(self.log_path):
            send_error_log(self.chat, self.log_path, self.msg_id, self.thread)

    def build(self):
        cfg = self.cfg
        cwd = os.path.expanduser(cfg.get('cwd', '~/y'))
        cmd = cfg['cmd']
        cmd = [os.path.expanduser(c) for c in (cmd if isinstance(cmd, list) else ['bash', '-c', cmd])]
        env = dict(os.environ, TERM='dumb')
        env.pop('BOT_TOKEN', None)
        self.stage = 'Building'
        self.update(force=True)
        start = time.time()
        with open(self.log_path, 'wb') as log:
            self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)

            def tick(proc=self.proc):
                while proc.poll() is None:
                    time.sleep(EDIT_EVERY)
                    self.update()
            threading.Thread(target=tick, daemon=True).start()
            buf = b''
            while True:
                chunk = self.proc.stdout.read1(65536)
                if not chunk:
                    break
                log.write(chunk)
                buf += chunk
                parts = re.split(b'[\\r\\n]', buf)
                buf = parts.pop()
                for raw in parts:
                    self.parse_line(ANSI_RE.sub('', raw.decode(errors='replace')).strip())
                self.update()
            rc = self.proc.wait()
        self.finished = time.time()
        if self.cancelled:
            self.result, self.stage = ('cancelled', 'Cancelled')
            return False
        out_dir = os.path.expanduser(cfg['out_dir'])
        if not os.path.isabs(out_dir):
            out_dir = os.path.join(cwd, out_dir)
        fresh = [p for p in glob.glob(os.path.join(out_dir, cfg.get('zip_glob', '*.zip'))) if os.path.getmtime(p) >= start]
        if not fresh:
            self.result, self.stage = ('fail', f'Build failed (exit {rc})' if rc else 'Build failed (no new zip)')
            self.error = extract_error(self.log_path)
            return False
        zips = sorted(fresh, key=os.path.getmtime)
        names = [os.path.basename(z) for z in zips]
        self.missing = [v for v in self.variants if not any((v in n for n in names))]
        if self.missing:
            self.error = extract_error(self.log_path)
        extras = []
        for fn in cfg.get('extra_files', []):
            extras += [p for p in glob.glob(os.path.join(out_dir, fn)) if os.path.getmtime(p) >= start and p not in zips]
        self.zips = self.stage_zips(zips + sorted(extras))
        self.sizes = {z: os.path.getsize(z) for z in self.zips if os.path.exists(z)}
        self.pct = 100.0
        self.done_n = self.total_n
        self.finished = None
        return True

    def stage_zips(self, zips):
        self.store_dir = os.path.join(BUILDS_DIR, f'{self.device}-{uuid.uuid4().hex[:6]}')
        os.makedirs(self.store_dir, exist_ok=True)
        staged = []
        for z in zips:
            dst = os.path.join(self.store_dir, os.path.basename(z))
            try:
                shutil.move(z, dst)
            except OSError as e:
                print(f'[stage] move {z} failed: {e}; uploading in place')
                staged.append(z)
                continue
            staged.append(dst)
        return staged

    def parse_line(self, line):
        if self.variant_re:
            v = self.variant_re.search(line)
            if v and v[1] != self.variant:
                self.variant = v[1]
                self.phase, self.setup_step = ('Setting up (lunch)', '')
                self.done_n = self.total_n = 0
                self.pct, self.eta = (0.0, '')
                return
        m = PROGRESS_RE.search(line)
        if m:
            done, total, desc = (int(m[1]), int(m[2]), m[4])
            if self.phase != 'Compiling' and phase_for(desc) != 'Compiling':
                self.phase = phase_for(desc)
                self.setup_step = f'{done}/{total}'
                self.action = desc
                return
            self.phase = 'Compiling'
            self.done_n, self.total_n = (done, total)
            self.pct = done * 100 / total if total else 0
            self.eta = m[3] or ''
            self.action = desc
        elif line.startswith('#### ') or 'Starting ninja' in line or 'lunch' in line.lower()[:10]:
            self.action = line

    def upload(self):
        self.stage = 'Uploading to mirror'
        self.update(force=True)
        failed = []
        for z in self.zips:
            self.uploading, self.up_pct, self.up_speed = (z, 0, 0)
            self.update(force=True)
            subdir = os.path.basename(self.store_dir) if self.store_dir else ''
            for attempt in range(3):
                try:
                    self.links[z] = mirror_upload(z, self.on_upload_progress, subdir)
                    break
                except Exception as e:
                    print(f'[upload] {os.path.basename(z)} attempt {attempt + 1}: {e}')
                    time.sleep(5)
            else:
                failed.append(z)
        self.uploading = None
        self.finished = time.time()
        if failed:
            self.result, self.stage = ('fail', 'Upload failed')
            self.error = 'Not uploaded (still on disk):\n' + '\n'.join(failed)
        elif self.missing:
            self.result, self.stage = ('fail', f"Partial: {', '.join(self.missing)} failed to build")
        else:
            self.stage = 'Done'
        if not failed and self.store_dir and os.path.isdir(self.store_dir):
            shutil.rmtree(self.store_dir, ignore_errors=True)

    def on_upload_progress(self, sent, total, speed):
        self.up_pct = sent * 100 / total if total else 0
        self.up_speed = speed
        self.update()

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGINT)
            threading.Timer(20, lambda: self.proc.poll() is None and os.killpg(self.proc.pid, signal.SIGKILL)).start()

def extract_error(log_path, max_lines=25):
    with open(log_path, 'rb') as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 2000000))
        lines = ANSI_RE.sub('', f.read().decode(errors='replace')).splitlines()
    for i, l in enumerate(lines):
        if l.startswith('FAILED:'):
            return '\n'.join(lines[i:i + max_lines])
    return '\n'.join(lines[-max_lines:])

def send_error_log(chat, log_path, reply_to, thread=None):
    tail_path = log_path.replace('.log', '_tail.log')
    with open(log_path, 'rb') as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 3000000))
        data = f.read()
    with open(tail_path, 'wb') as f:
        f.write(data)
    send_file(chat, tail_path, caption='last part of the build log', reply_to=reply_to, thread=thread)

def _mirror_path(base, name, subdir):
    root = base.path if base.path.endswith('/') else base.path + '/'
    if subdir:
        root += urllib.parse.quote(subdir.strip('/')) + '/'
    return root + urllib.parse.quote(name)

def _ssh_opts():
    return ['-P', str(SFTP_PORT), '-i', SFTP_KEY, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile={SFTP_KNOWN_HOSTS}', '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4']

def mirror_upload(path, progress, subdir=''):
    name = os.path.basename(path)
    size = os.path.getsize(path)
    remote_dir = SFTP_REMOTE_DIR.rstrip('/') + ('/' + subdir.strip('/') if subdir else '')
    remote_path = remote_dir + '/' + name
    dest = f'{SFTP_USER}@{SFTP_HOST}'
    subprocess.run(['sftp', *_ssh_opts(), dest], input=f'-mkdir {remote_dir}\nbye\n'.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    cmd = ['scp', *_ssh_opts(), path, f'{dest}:{remote_path}']
    mfd, sfd = pty.openpty()
    proc = subprocess.Popen(cmd, stdout=sfd, stderr=sfd, stdin=subprocess.DEVNULL, close_fds=True)
    os.close(sfd)
    t0, tail = (time.time(), b'')
    try:
        while True:
            try:
                r, _, _ = select.select([mfd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if r:
                try:
                    chunk = os.read(mfd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                tail = (tail + chunk)[-256:]
                m = list(re.finditer(b'(\\d{1,3})%', tail))
                if m:
                    pct = min(int(m[-1].group(1)), 100)
                    sent = pct * size // 100
                    progress(sent, size, sent / max(time.time() - t0, 0.001))
            elif proc.poll() is not None:
                break
    finally:
        os.close(mfd)
    if proc.wait() != 0:
        raise RuntimeError(f'scp exited {proc.returncode} uploading {name}')
    progress(size, size, size / max(time.time() - t0, 0.001))
    pub = urllib.parse.urlsplit(MIRROR_URL)
    return urllib.parse.urlunsplit((pub.scheme, pub.netloc, _mirror_path(pub, name, subdir), '', ''))

def _q(p):
    return '"' + p.replace('"', '').replace('\n', '').replace('\r', '') + '"'

def _within(path):
    root = SFTP_REMOTE_DIR.rstrip('/')
    p = os.path.normpath(path)
    return p == root or p.startswith(root + '/')

def _sftp_batch(commands, timeout=180):
    p = subprocess.run(['sftp', *_ssh_opts(), f'{SFTP_USER}@{SFTP_HOST}'], input=('\n'.join(commands) + '\nbye\n').encode(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    return (p.returncode, p.stdout.decode(errors='replace'))

def mirror_ls(path):
    _, out = _sftp_batch([f'ls -l {_q(path)}'])
    entries = []
    for line in out.splitlines():
        if not line or line[0] not in '-dlbcps':
            continue
        parts = line.split(None, 8)
        if len(parts) < 9 or not parts[4].isdigit():
            continue
        name = os.path.basename(parts[8].rstrip('/'))
        if name in ('.', '..'):
            continue
        entries.append((name, int(parts[4]), parts[0][0] == 'd'))
    entries.sort(key=lambda e: (not e[2], e[0].lower()))
    return entries

def mirror_delete(path, is_dir):
    if not _within(path) or os.path.normpath(path) == SFTP_REMOTE_DIR.rstrip('/'):
        return (False, 'refused: outside mirror dir')
    if not is_dir:
        rc, out = _sftp_batch([f'rm {_q(path)}'])
        return (rc == 0, out)
    cmds = []

    def walk(p):
        for name, _, d in mirror_ls(p):
            child = p.rstrip('/') + '/' + name
            if d:
                walk(child)
                cmds.append(f'rmdir {_q(child)}')
            else:
                cmds.append(f'rm {_q(child)}')
    walk(path)
    cmds.append(f'rmdir {_q(path)}')
    rc, out = _sftp_batch(cmds, timeout=600)
    return (rc == 0, out)
_nav = {}
_nav_seq = 0

def _tok(path, is_dir):
    global _nav_seq
    _nav_seq += 1
    t = str(_nav_seq)
    _nav[t] = (path, is_dir)
    if len(_nav) > 4000:
        for k in list(_nav)[:2000]:
            _nav.pop(k, None)
    return t

def _btn(text, action, path, is_dir):
    return {'text': text[:60], 'callback_data': f'{action}:{_tok(path, is_dir)}'}

def storage_view(path):
    entries = mirror_ls(path)
    total = sum((sz for _, sz, d in entries if not d))
    head = [f'<b>Mirror</b> <code>{html.escape(path)}</code>', f'{len(entries)} item(s)' + (f' · {fmt_size(total)} in files here' if total else '')]
    rows = []
    for name, size, is_dir in entries[:50]:
        child = path.rstrip('/') + '/' + name
        if is_dir:
            rows.append([_btn(f'[dir] {name}', 'nav', child, True)])
        else:
            rows.append([_btn(f'{name} · {fmt_size(size)}', 'del', child, False)])
    root = SFTP_REMOTE_DIR.rstrip('/')
    ctrl = []
    if path.rstrip('/') != root:
        ctrl.append(_btn('Up', 'nav', os.path.dirname(path.rstrip('/')) or root, True))
    ctrl.append(_btn('Refresh', 'nav', path, True))
    if path.rstrip('/') != root:
        ctrl.append(_btn('Delete this folder', 'del', path, True))
    rows.append(ctrl)
    hint = '\n\nTap a folder to open, or a file / Delete to remove.' if entries else '\n\n<i>(empty)</i>'
    return ('\n'.join(head) + hint, {'inline_keyboard': rows})

def confirm_view(tok):
    path, is_dir = _nav[tok]
    kind = 'folder and everything inside it' if is_dir else 'file'
    parent = os.path.dirname(path.rstrip('/')) or SFTP_REMOTE_DIR.rstrip('/')
    kb = {'inline_keyboard': [[{'text': 'Yes, delete', 'callback_data': f'delgo:{tok}'}, {'text': 'Cancel', 'callback_data': f'nav:{_tok(parent, True)}'}]]}
    return (f'Delete this {kind}?\n<code>{html.escape(path)}</code>', kb)

def handle_callback(cq):
    uid = cq['from']['id']
    msg = cq.get('message') or {}
    chat, mid = (msg.get('chat', {}).get('id'), msg.get('message_id'))
    data = cq.get('data', '')
    if uid not in ALLOWED:
        return answer_cb(cq['id'], 'Not allowed', alert=True)
    action, _, tok = data.partition(':')
    item = _nav.get(tok)
    if item is None:
        return answer_cb(cq['id'], 'This menu expired — send /storage again', alert=True)
    path, is_dir = item
    if not _within(path):
        return answer_cb(cq['id'], 'Refused (outside mirror dir)', alert=True)
    try:
        if action == 'nav':
            text, kb = storage_view(path)
            edit(chat, mid, text, reply_markup=kb)
            answer_cb(cq['id'])
        elif action == 'del':
            text, kb = confirm_view(tok)
            edit(chat, mid, text, reply_markup=kb)
            answer_cb(cq['id'])
        elif action == 'delgo':
            ok, out = mirror_delete(path, is_dir)
            parent = os.path.dirname(path.rstrip('/')) or SFTP_REMOTE_DIR.rstrip('/')
            text, kb = storage_view(parent)
            note = 'Deleted.' if ok else f'Delete failed ({html.escape(out[-120:].strip())})'
            edit(chat, mid, note + '\n\n' + text, reply_markup=kb)
            answer_cb(cq['id'], note[:190], alert=not ok)
        else:
            answer_cb(cq['id'])
    except Exception as e:
        print(f'[storage] {e!r}')
        answer_cb(cq['id'], f'Error: {str(e)[:180]}', alert=True)
current = None
BOT_USERNAME = ''
lock = threading.RLock()
BOOT = time.time()
QUEUE_FILE = os.path.join(HERE, 'queue.json')

def load_state():
    try:
        with open(QUEUE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if isinstance(data, list):
        data = {'pending': data}
    return data
_state = load_state()
queue = _state.get('pending', [])
running = _state.get('running')
tg_offset = _state.get('offset')
had_offset = tg_offset is not None
if running:
    queue.insert(0, running)
    running = None

def save_queue():
    tmp = QUEUE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'running': running, 'pending': queue, 'offset': tg_offset}, f)
    os.replace(tmp, QUEUE_FILE)

def busy():
    return current is not None and current.result is None

def start_next():
    global current, last_log, running
    with lock:
        while queue and (not busy()):
            e = queue.pop(0)
            cfg = load_devices().get(e['rom'], {}).get(e['device'])
            if not cfg:
                save_queue()
                send(e['chat'], thread=e.get('thread'), text=f"Skipping queued {html.escape(e['rom'])} {html.escape(e['device'])}: no longer in devices.json")
                continue
            running = e
            save_queue()
            current = Job(e['chat'], e['reply_to'], e['rom'], e['device'], cfg, e['user'], e.get('thread'))
            last_log = current.log_path
            print(f"[build] starting {e['rom']} {e['device']}: {cfg['cmd']} in {cfg.get('cwd')} ({len(queue)} left in queue)")
            threading.Thread(target=run_then_next, args=(current,), daemon=True).start()

def run_then_next(job):
    global running
    try:
        job.run()
    finally:
        with lock:
            running = None
            save_queue()
        start_next()
HELP = '<b>Build bot</b>\n/build &lt;rom&gt; &lt;device&gt; — start a build\n/status — bot ping and uptime\n/queue — show queued builds\n/cancel — stop current build (next in queue starts)\n/cancel &lt;n&gt; — remove build #n from the queue\n/devices — list configured targets\n/log — send tail of current/last build log\n/storage — browse the mirror; tap to open folders or delete files/folders\n/id — show your Telegram user id'
last_log = None
COMMANDS = {'/id', '/start', '/help', '/devices', '/build', '/queue', '/status', '/cancel', '/log', '/storage'}

def handle(msg):
    global current, last_log
    text = (msg.get('text') or '').strip()
    if not text.startswith('/'):
        return
    if not had_offset and msg.get('date', 0) < BOOT - 30:
        print(f'[handle] skipping stale command from before startup: {text!r}')
        return
    chat, user = (msg['chat']['id'], msg['from'])
    thread = msg.get('message_thread_id') if msg.get('is_topic_message') else None
    uid = user['id']
    print(f'[cmd] user={uid} chat={chat} {text!r}')
    name = user.get('username') and '@' + user['username'] or user.get('first_name', str(uid))
    parts = text.split()
    cmd, _, target = parts[0].partition('@')
    cmd = cmd.lower()
    if target and target.lower() != BOT_USERNAME.lower():
        return
    args = [a.lower() for a in parts[1:]]
    if cmd not in COMMANDS:
        return
    if cmd == '/id':
        return send(chat, thread=thread, text=f'Your user id: <code>{uid}</code>\nChat id: <code>{chat}</code>')
    if uid not in ALLOWED:
        return send(chat, thread=thread, text=f'Not allowed. Add <code>{uid}</code> to ALLOWED_USERS in config.env and restart.')
    if cmd in ('/start', '/help'):
        return send(chat, thread=thread, text=HELP)
    if cmd == '/devices':
        devs = load_devices()
        rows = [f'• <code>/build {r} {d}</code>' for r, ds in devs.items() for d in ds]
        return send(chat, thread=thread, text='\n'.join(rows) or 'No devices configured')
    if cmd == '/build':
        if len(args) != 2:
            return send(chat, thread=thread, text='Usage: <code>/build yaap vayu</code>', reply_to=msg['message_id'])
        rom, device = args
        cfg = load_devices().get(rom, {}).get(device)
        if not cfg:
            return send(chat, thread=thread, text=f'Unknown target <code>{html.escape(rom)} {html.escape(device)}</code>. See /devices', reply_to=msg['message_id'])
        with lock:
            queue.append({'chat': chat, 'reply_to': msg['message_id'], 'rom': rom, 'device': device, 'user': name, 'thread': thread})
            save_queue()
            if busy():
                print(f'[queue] added {rom} {device} at #{len(queue)}')
                return send(chat, thread=thread, text=f'Queued <b>{html.escape(rom)} {html.escape(device)}</b> at #{len(queue)}. Running now: {html.escape(current.rom)} {html.escape(current.device)}. /queue', reply_to=msg['message_id'])
            start_next()
        return
    if cmd == '/queue':
        with lock:
            rows = []
            if busy():
                rows.append(f'Running: <b>{html.escape(current.rom)} {html.escape(current.device)}</b> ({html.escape(current.stage)}, {fmt_time(time.time() - current.started)})')
            rows += [f"#{i}  {html.escape(e['rom'])} {html.escape(e['device'])}  <i>by {html.escape(e['user'])}</i>" for i, e in enumerate(queue, 1)]
        return send(chat, thread=thread, text='\n'.join(rows) or 'Queue is empty, nothing running.')
    if cmd == '/status':
        t0 = time.time()
        tg('getMe')
        ping = (time.time() - t0) * 1000
        return send(chat, thread=thread, text=f'<b>Bot is up!</b>\n<b>Ping:</b> {ping:.0f} ms\n<b>Uptime:</b> {fmt_time(time.time() - BOOT)}')
    if cmd == '/cancel' and args:
        with lock:
            if not args[0].isdigit() or not 1 <= int(args[0]) <= len(queue):
                return send(chat, thread=thread, text=f'No queued build #{html.escape(args[0])}. See /queue')
            e = queue.pop(int(args[0]) - 1)
            save_queue()
        return send(chat, thread=thread, text=f"Removed {html.escape(e['rom'])} {html.escape(e['device'])} from the queue.")
    if cmd == '/cancel':
        if not current or current.result is not None:
            return send(chat, thread=thread, text='Nothing running.')
        if current.stage == 'Uploading to mirror':
            return send(chat, thread=thread, text="Build finished; upload in progress, can't cancel that part.")
        current.cancel()
        return send(chat, thread=thread, text='Cancelling…')
    if cmd == '/log':
        if not last_log or not os.path.exists(last_log):
            return send(chat, thread=thread, text='No log yet.')
        return send_error_log(chat, last_log, msg['message_id'], thread)
    if cmd == '/storage':
        try:
            text, kb = storage_view(SFTP_REMOTE_DIR)
        except Exception as e:
            return send(chat, thread=thread, text=f'Storage error: {html.escape(str(e))}')
        return send(chat, text, thread=thread, reply_markup=kb)

def main():
    threading.excepthook = lambda a: print(f'[thread] {a.exc_type.__name__}: {a.exc_value}')
    while not (me := tg('getMe')):
        print('getMe failed (bad token or no network), retrying in 10s')
        time.sleep(10)
    global BOT_USERNAME
    BOT_USERNAME = me['username']
    print(f"running as @{me['username']}, allowed users: {sorted(ALLOWED) or 'NONE (use /id)'}")
    global tg_offset
    if queue:
        print(f'[queue] resuming {len(queue)} queued build(s)')
        start_next()
    while True:
        try:
            updates = tg_updates(tg_offset)
        except Exception as e:
            print(f'[poll] {e!r}')
            updates = None
        for u in updates or []:
            with lock:
                tg_offset = u['update_id'] + 1
                save_queue()
            m = u.get('message') or {}
            print(f"[update] {u['update_id']} chat={m.get('chat', {}).get('id')} topic={m.get('message_thread_id')} {(m.get('text') or '')[:60]!r}")
            if 'message' in u:
                try:
                    handle(u['message'])
                except Exception as e:
                    print(f'[handle] {e!r}')
            elif 'callback_query' in u:
                try:
                    handle_callback(u['callback_query'])
                except Exception as e:
                    print(f'[callback] {e!r}')
        if updates is None:
            time.sleep(3)

def tg_updates(offset):
    params = {'timeout': 50, 'allowed_updates': json.dumps(['message', 'callback_query'])}
    if offset:
        params['offset'] = offset
    req = urllib.request.Request(API + 'getUpdates', data=urllib.parse.urlencode(params).encode())
    try:
        with urllib.request.urlopen(req, timeout=65) as r:
            return json.load(r).get('result', [])
    except Exception as e:
        print(f'[poll] {e}')
        return None
if __name__ == '__main__':
    main()
