"""Batch speech-to-text for generated voiceover mp3s via the audio.cpp server.

For every mp3 under the sounds dir it converts the file to 16 kHz mono wav
(the server only accepts WAV), POSTs it to /v1/audio/transcriptions and
stores the recognized text in a sidecar JSON next to the mp3:
    <name>.json  {voice, npc, text, recognized, verdict}

Files whose sidecar already has a recognized text are skipped (--force to redo).

Example:
    python scripts/stt_batch.py --url https://HOST
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_ffmpeg():
    import shutil
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        sys.exit('ffmpeg not found in PATH')
    return ffmpeg


def load_manifests(samples_dir):
    """file_name (e.g. quests/f-25-accept.mp3) -> {voice, npc, text}."""
    info = {}
    if not os.path.isdir(samples_dir):
        return info
    for voice in os.listdir(samples_dir):
        manifest = os.path.join(samples_dir, voice, 'manifest.jsonl')
        if not os.path.isfile(manifest):
            continue
        with open(manifest, encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                m = json.loads(line)
                rel = m['subfolder'] + '/' + m['file_name']
                info.setdefault(rel, {'voice': voice, 'npc': m.get('npc_name', ''),
                                      'text': m.get('text', '')})
    return info


def list_mp3s(sounds_dir):
    out = []
    for root, _dirs, files in os.walk(sounds_dir):
        for name in sorted(files):
            if name.lower().endswith('.mp3'):
                out.append(os.path.relpath(os.path.join(root, name), sounds_dir))
    return out


def sidecar_path(sounds_dir, rel):
    # rel ends with .mp3; drop it so sidecar is <name>.json
    return os.path.join(sounds_dir, os.path.splitext(rel)[0] + '.json')


def load_sidecar(sounds_dir, rel, meta):
    # manifest is the source of truth for voice/npc/text; the sidecar only
    # contributes recognized text and the verdict
    rec = {'voice': meta.get('voice', ''), 'npc': meta.get('npc', ''),
           'text': meta.get('text', ''), 'recognized': '', 'verdict': None}
    path = sidecar_path(sounds_dir, rel)
    if os.path.isfile(path):
        try:
            stored = json.load(open(path, encoding='utf-8'))
        except (json.JSONDecodeError, OSError) as e:
            print(f'  bad sidecar, rewriting: {e}', file=sys.stderr)
            return rec
        if stored.get('recognized'):
            rec['recognized'] = stored['recognized']
        if stored.get('verdict'):
            rec['verdict'] = stored['verdict']
    return rec


def save_sidecar(sounds_dir, rel, rec):
    with open(sidecar_path(sounds_dir, rel), 'w', encoding='utf-8') as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)


def to_wav(ffmpeg, mp3_path, wav_path):
    subprocess.run([ffmpeg, '-y', '-v', 'error', '-i', mp3_path,
                    '-ar', '16000', '-ac', '1', wav_path], check=True)


def transcribe(session, url, model, wav_path, timeout, retries, retry_wait):
    last = None
    for attempt in range(1, retries + 1):
        try:
            with open(wav_path, 'rb') as f:
                r = session.post(url, files={'file': (os.path.basename(wav_path), f, 'audio/wav')},
                                 data={'model': model}, timeout=timeout)
            if r.status_code == 200:
                return r.json().get('text', '').strip(), None
            last = f'HTTP {r.status_code}: {r.text[:200]}'
        except (requests.RequestException, OSError) as e:
            last = f'{type(e).__name__}: {e}'
        print(f'  attempt {attempt}/{retries} failed: {last}', file=sys.stderr)
        if attempt < retries:
            time.sleep(retry_wait)
    return None, last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sounds-dir', default=os.path.join(ROOT, 'AI_VoiceOverData_Vanilla', 'generated', 'sounds'))
    ap.add_argument('--samples-dir', default=os.path.join(ROOT, 'samples'))
    ap.add_argument('--url', required=True, help='audio.cpp server base URL')
    ap.add_argument('--model', default='parakeet-v3')
    ap.add_argument('--force', action='store_true', help='re-recognize files that already have text')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--timeout', type=int, default=120)
    ap.add_argument('--retries', type=int, default=3)
    ap.add_argument('--retry-wait', type=float, default=5.0)
    args = ap.parse_args()

    ffmpeg = find_ffmpeg()
    url = args.url.rstrip('/') + '/v1/audio/transcriptions'
    manifests = load_manifests(args.samples_dir)
    files = list_mp3s(args.sounds_dir)
    print(f'found {len(files)} mp3')

    session = requests.Session()
    done = skipped = errors = 0
    started = time.time()
    for i, rel in enumerate(files, 1):
        rec = load_sidecar(args.sounds_dir, rel, manifests.get(rel, {}))
        if rec['recognized'] and not args.force:
            skipped += 1
            continue
        if args.limit and done >= args.limit:
            break
        wav = os.path.join(tempfile.gettempdir(), 'stt_batch_tmp.wav')
        try:
            to_wav(ffmpeg, os.path.join(args.sounds_dir, rel), wav)
            text, err = transcribe(session, url, args.model, wav,
                                   args.timeout, args.retries, args.retry_wait)
        finally:
            if os.path.isfile(wav):
                os.remove(wav)
        if text is None:
            errors += 1
            print(f'[{i}/{len(files)}] ERROR {rel}: {err}', file=sys.stderr)
            continue
        rec['recognized'] = text
        save_sidecar(args.sounds_dir, rel, rec)
        done += 1
        print(f'[{i}/{len(files)}] {rel}: {text[:60]}')
    print(f'finished: {done} recognized, {skipped} skipped, {errors} errors '
          f'in {time.time() - started:.0f}s')


if __name__ == '__main__':
    main()
