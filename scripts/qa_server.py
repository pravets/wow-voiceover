"""Local QA workspace for reviewing generated voiceover.

Serves a single page with per-file audio players, filters (voice / npc /
unchecked only) and verdict buttons. Sidecar JSONs (<name>.json next to each
mp3) store {voice, npc, text, recognized, verdict}; the STT button recognizes
one file through the audio.cpp server on demand.

    python scripts/qa_server.py --url https://HOST
"""
import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ARGS = None  # filled in main()

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Проверка озвучки</title>
<style>
 body{font:14px system-ui;margin:0;padding:12px;background:#fafafa}
 .bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:12px;
      position:sticky;top:0;background:#fafafa;padding:8px 0;border-bottom:1px solid #ddd;z-index:5}
 select,input[type=text]{padding:4px 8px}
 .cnt{color:#666}
 .item{border:1px solid #ddd;border-radius:8px;padding:10px;margin-bottom:10px;background:#fff}
 .item.ok{border-color:#9c9} .item.warn{border-color:#fc6} .item.bad{border-color:#f99}
 .meta{color:#666;font-size:12px}
 .texts{margin:6px 0}
 .texts textarea{width:100%;box-sizing:border-box;font:13px/1.35 inherit}
 .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 button{padding:4px 10px;cursor:pointer}
 button.cur{font-weight:bold}
 button:disabled{opacity:.35;cursor:not-allowed}
 .verdict-ok.cur{background:#dfd}.verdict-stress.cur{background:#fec}.verdict-voice.cur{background:#fdd}
 audio{vertical-align:middle}
 .stt{color:#333}
</style></head><body>
<div class="bar">
 <label>Голос <select id="fv"><option value="">все</option></select></label>
 <label>NPC <select id="fn"><option value="">все</option></select></label>
 <label><input type="checkbox" id="funchecked"> только непроверенные</label>
 <label><input type="checkbox" id="fwork"> непроверенные и с замечаниями</label>
 <label><input type="checkbox" id="fall"> показывать без mp3</label>
 <label>Seed <input type="text" id="fseed" size="6" placeholder="по умолчанию"></label>
 <span class="cnt" id="cnt"></span>
 <button id="pprev">←</button> <span class="cnt" id="ppage"></span> <button id="pnext">→</button>
 <label><input type="checkbox" id="pall"> все</label>
</div>
<div id="list"></div>
<script>
let DATA = [], page = 0;
const PSIZE = 100;
const $ = id => document.getElementById(id);
const esc = s => (s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const vmap = {ok:'✅ ок', stress:'⚠️ ударения', voice:'👎 голос'};

const fill = (sel, vals, cur, cnt) => {
  const num = {};
  if (cnt) vals.forEach(v => num[v] = (num[v] || 0) + 1);
  sel.innerHTML = '<option value="">все</option>' +
    [...new Set(vals)].sort().map(v => `<option value="${esc(v)}"${v === cur ? ' selected' : ''}>${esc(v)}${cnt ? ` (${num[v]})` : ''}</option>`).join('');
};

async function load(){
  DATA = await (await fetch('/api/files')).json();
  refill();
  render();
}
// cascading lookups: NPC options respect selected voice and vice versa
function refill(){
  page = 0;
  const v = $('fv').value, n = $('fn').value, un = $('funchecked').checked,
        all = $('fall').checked, work = $('fwork').checked;
  const pool = DATA.filter(d => (all || d.audio) && (!work || d.verdict !== 'ok'));
  fill($('fn'), pool.filter(d => (!v || d.voice === v) && (!un || !d.verdict)).map(d => d.npc), n, true);
  fill($('fv'), pool.filter(d => !n || d.npc === n).map(d => d.voice), v);
}
function filtered(){
  const v = $('fv').value, n = $('fn').value, un = $('funchecked').checked,
        all = $('fall').checked, work = $('fwork').checked;
  return DATA.filter(d => (!v || d.voice === v) && (!n || d.npc === n) && (!un || !d.verdict)
                          && (!work || d.verdict !== 'ok') && (all || d.audio));
}
function render(){
  const rows = filtered();
  const pages = Math.max(1, Math.ceil(rows.length / PSIZE));
  if (page >= pages) page = pages - 1;
  const shown = $('pall').checked ? rows : rows.slice(page * PSIZE, (page + 1) * PSIZE);
  $('cnt').textContent = `${rows.length} из ${DATA.length}`;
  $('ppage').textContent = $('pall').checked ? 'все' : `${page + 1} / ${pages}`;
  $('list').innerHTML = shown.map(d => `
   <div class="item ${d.verdict ? ' ' + ({ok:'ok',stress:'warn',voice:'bad'}[d.verdict] || '') : ''}" id="i-${d.id}">
    <div class="meta">${esc(d.id)} · ${esc(d.npc)}${d.audio ? '' : ' · <b>нет mp3</b>'}</div>
    <div class="row">
     ${d.audio ? `<audio controls preload="none" src="/audio?rel=${encodeURIComponent(d.rel)}"></audio>`
               : ''}
     <button class="verdict-ok${d.verdict==='ok'?' cur':''}"   onclick="setVerdict('${d.id}','ok')" ${d.audio?'':'disabled'}>✅ всё хорошо</button>
     <button class="verdict-stress${d.verdict==='stress'?' cur':''}" onclick="setVerdict('${d.id}','stress')" ${d.audio?'':'disabled'}>⚠️ ударения</button>
     <button class="verdict-voice${d.verdict==='voice'?' cur':''}" onclick="setVerdict('${d.id}','voice')" ${d.audio?'':'disabled'}>👎 голос/интонации</button>
     <button onclick="stt('${d.id}')" ${d.audio?'':'disabled'}>🎙 распознать</button>
     <button onclick="openAu('${d.id}')" ${d.audio?'':'disabled'}>🎛 audacity</button>
     <button onclick="copyPath('${d.id}')" title="скопировать полный путь к mp3">📋</button>
     <button onclick="regen('${d.id}')" ${d.verdict === 'stress' || d.verdict === 'voice' ? '' : 'disabled'}>♻ перегенерировать</button>
     <span class="stt" id="stt-${d.id}"></span>
    </div>
    <div class="texts">
     <div><b>Озвучено:</b></div>
      <textarea id="txt-${d.id}" rows="2" placeholder="текст для генерации">${esc(d.text)}</textarea>
     <textarea id="rec-${d.id}" rows="2" placeholder="распознанный текст">${esc(d.recognized)}</textarea>
    </div>
   </div>`).join('');
}
async function save(d){
  d.recognized = $('rec-' + d.id).value;
  await fetch('/api/save', {method:'POST', headers:{'Content-Type':'application/json'},
                            body: JSON.stringify({file: d.id, verdict: d.verdict, recognized: d.recognized})});
}
async function saveText(d){
  const r = await (await fetch('/api/text', {method:'POST', headers:{'Content-Type':'application/json'},
                                             body: JSON.stringify({file: d.id, text: d.text})})).json();
  if (r.error) alert('не сохранено: ' + r.error);
}
async function setVerdict(id, v){
  const d = DATA.find(x => x.id === id);
  d.verdict = d.verdict === v ? null : v;
  await save(d);
  render();
}
async function stt(id){
  const d = DATA.find(x => x.id === id);
  const s = $('stt-' + id); s.textContent = 'распознаю…';
  const r = await (await fetch('/api/stt', {method:'POST', headers:{'Content-Type':'application/json'},
                                            body: JSON.stringify({file: d.id})})).json();
  d.recognized = r.recognized || '';
  $('rec-' + id).value = d.recognized;
  s.textContent = 'готово';
  await save(d);
}
function copyPath(id){
  const d = DATA.find(x => x.id === id);
  navigator.clipboard.writeText(d.path);
}
async function openAu(id){
  const d = DATA.find(x => x.id === id);
  const r = await (await fetch('/api/open', {method:'POST', headers:{'Content-Type':'application/json'},
                                             body: JSON.stringify({file: d.id})})).json();
  if (r.error) alert(r.error);
}
async function regen(id){
  const d = DATA.find(x => x.id === id);
  const s = $('stt-' + id);
  if (!confirm('Перегенерировать ' + d.rel + '? Текущий mp3 будет перезаписан.')) return;
  s.textContent = 'генерирую…';
  const seed = $('fseed').value.trim();
  const r = await (await fetch('/api/regen', {method:'POST', headers:{'Content-Type':'application/json'},
                                              body: JSON.stringify({file: d.id, seed: seed ? Number(seed) : undefined})})).json();
  if (r.error) { s.textContent = 'ошибка: ' + r.error; return; }
  d.verdict = null; d.recognized = ''; d.audio = true;
  s.textContent = 'перегенерировано';
  render();
}
$('fv').onchange = $('fn').onchange = () => { refill(); render(); };
$('funchecked').onchange = $('fall').onchange = $('fwork').onchange = () => { refill(); render(); };
$('pprev').onclick = () => { if (page > 0) { page--; render(); } };
$('pnext').onclick = () => { page++; render(); };
$('pall').onchange = () => { page = 0; render(); };
document.addEventListener('change', e => {
  if (e.target.tagName !== 'TEXTAREA') return;
  const d = DATA.find(x => 'rec-' + x.id === e.target.id || 'txt-' + x.id === e.target.id);
  if (!d) return;
  if (e.target.id === 'txt-' + d.id) { d.text = e.target.value; saveText(d); }
  else save(d);
});
load();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(n) or b'{}') if n else {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/':
            page = PAGE.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        elif u.path == '/api/files':
            self._json(list_files())
        elif u.path == '/audio':
            rel = parse_qs(u.query).get('rel', [''])[0]
            full = os.path.realpath(os.path.join(ARGS.sounds_dir, rel))
            if not full.startswith(os.path.realpath(ARGS.sounds_dir)) or not os.path.isfile(full):
                return self._json({'error': 'not found'}, 404)
            self.send_response(200)
            self.send_header('Content-Type', 'audio/mpeg')
            self.send_header('Content-Length', str(os.path.getsize(full)))
            self.end_headers()
            with open(full, 'rb') as f:
                shutil.copyfileobj(f, self.wfile)
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        body = self._read_body()
        id_ = body.get('file', '')
        if id_ not in MANIFESTS:
            return self._json({'error': 'bad file'}, 400)
        full = mp3_path(id_)
        if u.path == '/api/text':
            err = save_text(id_, str(body.get('text', '')))
            if err:
                return self._json({'error': err}, 400)
            return self._json({'ok': True})
        if u.path == '/api/open':
            if not ARGS.audacity:
                return self._json({'error': 'Audacity не найден, укажите --audacity <path>'}, 400)
            if not os.path.isfile(full):
                return self._json({'error': 'нет mp3'}, 400)
            subprocess.Popen([ARGS.audacity, full])
            return self._json({'ok': True})
        if u.path == '/api/save':
            rec = load_rec(id_)
            if 'verdict' in body:
                rec['verdict'] = body['verdict']
            if 'recognized' in body:
                rec['recognized'] = body['recognized']
            save_rec(id_, rec)
            self._json({'ok': True})
        elif u.path == '/api/stt':
            text, err = recognize_file(full)
            if text is None:
                return self._json({'error': err}, 502)
            rec = load_rec(id_)
            rec['recognized'] = text
            save_rec(id_, rec)
            self._json({'ok': True, 'recognized': text})
        elif u.path == '/api/regen':
            seed = body.get('seed')
            err = regenerate_file(id_, seed if isinstance(seed, int) else None)
            if err:
                return self._json({'error': err}, 502)
            self._json({'ok': True})
        else:
            self._json({'error': 'not found'}, 404)


def load_manifests():
    info = {}
    samples = ARGS.samples_dir
    if not os.path.isdir(samples):
        return info
    for voice in os.listdir(samples):
        manifest = os.path.join(samples, voice, 'manifest.jsonl')
        if not os.path.isfile(manifest):
            continue
        for line in open(manifest, encoding='utf-8'):
            if not line.strip():
                continue
            m = json.loads(line)
            # id is voice-scoped: different voices may share subfolder/file_name
            id_ = voice + '/' + m['subfolder'] + '/' + m['file_name']
            info[id_] = {'voice': voice, 'npc': m.get('npc_name', ''),
                         'text': m.get('text', '')}
    return info


def mp3_path(id_):
    """id = voice/rel; the mp3 lives in the shared sounds dir under rel."""
    return os.path.realpath(os.path.join(ARGS.sounds_dir, *id_.split('/')[1:]))


def _read_json(path):
    if not os.path.isfile(path):
        return None
    try:
        return json.load(open(path, encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return None


def load_rec(id_):
    # manifest wins for voice/npc/text; sidecar contributes recognized/verdict
    rec = {'voice': '', 'npc': '', 'text': '', 'recognized': '', 'verdict': None}
    rec.update(MANIFESTS.get(id_, {}))
    rel = id_.split('/', 1)[1] if '/' in id_ else id_
    # legacy sidecars sit next to the mp3 (plain rel); new ones are voice-scoped
    stored = _read_json(os.path.join(ARGS.sounds_dir, os.path.splitext(rel)[0] + '.json'))
    if stored and stored.get('voice') in ('', rec['voice']):
        if stored.get('recognized'):
            rec['recognized'] = stored['recognized']
        if stored.get('verdict'):
            rec['verdict'] = stored['verdict']
    stored = _read_json(os.path.join(ARGS.sounds_dir, id_ + '.json'))
    if stored:
        if stored.get('recognized'):
            rec['recognized'] = stored['recognized']
        if stored.get('verdict'):
            rec['verdict'] = stored['verdict']
    return rec


def save_rec(id_, rec):
    path = os.path.join(ARGS.sounds_dir, id_ + '.json')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)


def save_text(id_, text):
    """Fix the generation text in the voice manifest (regen and the batch
    pipeline both read it from there) and refresh the in-memory copy."""
    meta = MANIFESTS.get(id_)
    if not meta:
        return 'нет записи в манифесте для этого файла'
    rel = id_.split('/', 1)[1]
    path = os.path.join(ARGS.samples_dir, meta['voice'], 'manifest.jsonl')
    if not os.path.isfile(path):
        return f'манифест не найден: {path}'
    with open(path, encoding='utf-8') as f:
        lines = f.read().splitlines()
    out, found = [], False
    for line in lines:
        m = json.loads(line) if line.strip() else None
        if m and m.get('subfolder', '') + '/' + m.get('file_name', '') == rel:
            m['text'] = text
            line = json.dumps(m, ensure_ascii=False)
            found = True
        out.append(line)
    if not found:
        return 'запись не найдена в манифесте'
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')
    os.replace(tmp, path)
    meta['text'] = text
    return None


def list_files():
    out = []
    for id_, _rec in MANIFESTS.items():
        rec = load_rec(id_)
        out.append({'id': id_, 'rel': id_.split('/', 1)[1], 'voice': rec['voice'],
                    'npc': rec['npc'], 'text': rec['text'],
                    'recognized': rec['recognized'], 'verdict': rec['verdict'],
                    'audio': os.path.isfile(mp3_path(id_)),
                    'path': mp3_path(id_)})
    return out


def recognize_file(mp3_path):
    wav = os.path.join(tempfile.gettempdir(), 'qa_stt_tmp.wav')
    try:
        subprocess.run([ARGS.ffmpeg, '-y', '-v', 'error', '-i', mp3_path,
                        '-ar', '16000', '-ac', '1', wav], check=True)
        boundary = '----qa%016x' % os.getpid()
        name = os.path.basename(mp3_path)
        wav_bytes = open(wav, 'rb').read()
        parts = []
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{ARGS.model}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}.wav"\r\n'
                     f'Content-Type: audio/wav\r\n\r\n'.encode() + wav_bytes + b'\r\n')
        parts.append(f'--{boundary}--\r\n'.encode())
        payload = b''.join(parts)
        req = urllib.request.Request(ARGS.url.rstrip('/') + '/v1/audio/transcriptions', data=payload,
                                     headers=dict({'Content-Type': f'multipart/form-data; boundary={boundary}'},
                                                  **({'Authorization': 'Bearer ' + ARGS.api_key} if ARGS.api_key else {})))
        with urllib.request.urlopen(req, timeout=ARGS.timeout) as r:
            return json.loads(r.read()).get('text', '').strip(), None
    except (subprocess.CalledProcessError, OSError, urllib.error.URLError) as e:
        return None, str(e)
    finally:
        if os.path.isfile(wav):
            os.remove(wav)


def regenerate_file(id_, seed=None):
    """Re-synthesize one file from its manifest text and clear the review."""
    meta = MANIFESTS.get(id_)
    if not meta or not meta.get('voice'):
        return 'нет записи в манифесте для этого файла'
    mp3 = mp3_path(id_)
    voice_dir = os.path.join(ARGS.samples_dir, meta['voice'])
    ref_path = os.path.join(voice_dir, 'ref.wav')
    ref_txt_path = os.path.join(voice_dir, 'ref.txt')
    if not os.path.isfile(ref_path) or not os.path.isfile(ref_txt_path):
        return f'нет ref.wav/ref.txt для голоса {meta["voice"]}'
    text = meta.get('text', '').strip()
    if not text:
        return 'пустой текст в манифесте'
    try:
        ref_b64 = base64.b64encode(open(ref_path, 'rb').read()).decode('ascii')
        ref_text = open(ref_txt_path, encoding='utf-8').read().strip()
        payload = {'model': ARGS.tts_model, 'input': text,
                   'voice_ref': {'type': 'base64', 'data': ref_b64},
                   'reference_text': ref_text}
        if seed is not None:
            payload['seed'] = seed  # same field as audiocpp_batch.py
        body = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(ARGS.url.rstrip('/') + '/v1/audio/speech', data=body,
                                     headers=dict({'Content-Type': 'application/json'},
                                                  **({'Authorization': 'Bearer ' + ARGS.api_key} if ARGS.api_key else {})))
        with urllib.request.urlopen(req, timeout=ARGS.tts_timeout) as r:
            wav_bytes = r.read()
    except (OSError, urllib.error.URLError) as e:
        return f'TTS request failed: {e}'
    tmp_mp3 = mp3 + '.tmp.mp3'
    tmp_wav = os.path.join(tempfile.gettempdir(), 'qa_regen_tmp.wav')
    try:
        with open(tmp_wav, 'wb') as f:
            f.write(wav_bytes)
        subprocess.run([ARGS.ffmpeg, '-y', '-v', 'error', '-i', tmp_wav,
                        '-c:a', 'libmp3lame', '-b:a', '64k', '-ar', '24000',
                        '-ac', '1', tmp_mp3], check=True)
        os.makedirs(os.path.dirname(mp3), exist_ok=True)
        os.replace(tmp_mp3, mp3)
    except subprocess.CalledProcessError as e:
        return f'ffmpeg failed: {e}'
    except PermissionError:
        if os.path.isfile(tmp_mp3):
            os.remove(tmp_mp3)
        return 'mp3 занят другим процессом, закройте его'
    finally:
        for p in (tmp_wav, tmp_mp3):
            if os.path.isfile(p):
                os.remove(p)
    rec = load_rec(id_)
    rec['recognized'] = ''
    rec['verdict'] = None
    save_rec(id_, rec)
    return None


def main():
    global ARGS, MANIFESTS
    ap = argparse.ArgumentParser()
    ap.add_argument('--sounds-dir', default=os.path.join(ROOT, 'AI_VoiceOverData_Vanilla', 'generated', 'sounds'))
    ap.add_argument('--samples-dir', default=os.path.join(ROOT, 'samples'))
    ap.add_argument('--url', required=True, help='audio.cpp server base URL')
    ap.add_argument('--model', default='parakeet-v3')
    ap.add_argument('--tts-model', default='higgs-bf16')
    ap.add_argument('--tts-timeout', type=int, default=600,
                    help='regeneration request timeout, seconds')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--timeout', type=int, default=120, help='STT request timeout, seconds')
    ap.add_argument('--api-key', default=None, help='Bearer token for the audio server')
    ap.add_argument('--audacity', default=None,
                    help='path to Audacity.exe (auto-detected if omitted)')
    args = ap.parse_args()
    ARGS = args
    import shutil as sh
    ARGS.ffmpeg = sh.which('ffmpeg')
    if not ARGS.ffmpeg:
        sys.exit('ffmpeg not found in PATH')
    MANIFESTS = load_manifests()
    if not args.audacity:
        args.audacity = sh.which('audacity') or sh.which('audacity.exe')
    if not args.audacity:
        for p in (r'C:\Program Files\Audacity\Audacity.exe',
                  r'C:\Program Files (x86)\Audacity\Audacity.exe'):
            if os.path.isfile(p):
                args.audacity = p
                break
    print(f'audacity: {args.audacity or "not found"}')
    print(f'{len(MANIFESTS)} manifest entries, sounds dir: {ARGS.sounds_dir}')
    print(f'http://127.0.0.1:{args.port}  (Ctrl+C to stop)')
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
