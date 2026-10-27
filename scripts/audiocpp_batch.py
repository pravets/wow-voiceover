"""Voice a JSONL manifest through an audio.cpp server (POST /v1/audio/speech).

Zero-shot voice cloning: every request carries the reference WAV (base64) and
its transcript. The server returns 24 kHz mono WAV, which is converted to
mp3 64 kbps for the AI_VoiceOverData_Vanilla addon.

Example:
    python scripts/audiocpp_batch.py --manifest samples/tauren-male/manifest.jsonl \
        --out AI_VoiceOverData_Vanilla/generated/sounds \
        --ref samples/tauren-male/ref.wav --ref-text-file samples/tauren-male/ref.txt \
        --seed 1179406571 --limit 10
"""
import argparse
import base64
import json
import os
import shutil
import struct
import subprocess
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_ffmpeg() -> str:
    found = shutil.which('ffmpeg')
    if not found:
        sys.exit('ffmpeg not found in PATH; install it or add it to PATH.')
    return found


def wav_duration(data: bytes) -> float:
    """Duration of a RIFF/WAVE buffer in seconds (walks chunks)."""
    if len(data) < 12 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise ValueError('not a RIFF/WAVE payload')
    pos = 12
    byte_rate = None
    data_size = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack('<I', data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b'fmt ':
            byte_rate = struct.unpack('<I', body[8:12])[0]
        elif cid == b'data':
            data_size = size if pos + 8 + size <= len(data) else len(data) - pos - 8
            break
        pos += 8 + size + (size & 1)
    if not byte_rate or data_size is None:
        raise ValueError('wav header incomplete')
    return data_size / byte_rate


def load_state(path: str) -> dict:
    done = {}
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get('status') == 'done':
                    done[rec['file_name']] = rec
    return done


def target_path(out_dir: str, item: dict) -> str:
    return os.path.join(out_dir, item['subfolder'], item['file_name'])


def to_mp3(ffmpeg: str, src: str, dst: str):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + '.tmp.mp3'
    subprocess.run(
        [ffmpeg, '-y', '-v', 'error', '-i', src, '-c:a', 'libmp3lame',
         '-b:a', '64k', '-ar', '24000', '-ac', '1', tmp],
        check=True)
    os.replace(tmp, dst)


def synth_one(session: requests.Session, url: str, model: str, item: dict,
              ref_b64: str, ref_text: str, args, ffmpeg: str) -> dict:
    started = time.time()
    body = {
        'model': model,
        'input': item['text'],
        'voice_ref': {'type': 'base64', 'data': ref_b64},
        'reference_text': ref_text,
    }
    if args.seed is not None:
        body['seed'] = args.seed
    options = {}
    if args.temperature is not None:
        options['temperature'] = args.temperature
    if args.top_p is not None:
        options['top_p'] = args.top_p
    if args.top_k is not None:
        options['top_k'] = args.top_k
    if options:
        body['options'] = options

    last_err = None
    for attempt in range(1, args.retries + 1):
        try:
            r = session.post(url, json=body, timeout=args.timeout,
                             headers={'Content-Type': 'application/json'})
            if r.status_code == 200 and r.headers.get('content-type') != 'application/json':
                audio = r.content
                duration = wav_duration(audio)
                tmp_wav = os.path.join(args.out, '.tmp', item['file_name'] + '.wav')
                os.makedirs(os.path.dirname(tmp_wav), exist_ok=True)
                with open(tmp_wav, 'wb') as f:
                    f.write(audio)
                dst = target_path(args.out, item)
                to_mp3(ffmpeg, tmp_wav, dst)
                os.remove(tmp_wav)
                elapsed = time.time() - started
                return {'file_name': item['file_name'], 'status': 'done',
                        'duration': round(duration, 2), 'chars': item['chars'],
                        'seed': args.seed, 'elapsed_s': round(elapsed, 1)}
            last_err = f'HTTP {r.status_code}: {r.text[:300]}'
        except (requests.RequestException, ValueError,
                subprocess.CalledProcessError, OSError) as e:
            last_err = f'{type(e).__name__}: {e}'
        print(f'    attempt {attempt}/{args.retries} failed: {last_err}',
              file=sys.stderr)
        if attempt < args.retries:
            time.sleep(args.retry_wait)
    return {'file_name': item['file_name'], 'status': 'error',
            'error': last_err, 'chars': item['chars'], 'seed': args.seed}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--manifest', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--state', default=None,
                    help='state file (default: <manifest>.audiocpp.state.jsonl)')
    ap.add_argument('--ref', required=True, help='reference WAV for the voice')
    ap.add_argument('--ref-text-file', required=True,
                    help='text file with what is said in the reference')
    ap.add_argument('--url', required=True,
                    help='audio.cpp server base URL')
    ap.add_argument('--model', default='higgs-bf16')
    ap.add_argument('--seed', type=int, default=None)
    ap.add_argument('--temperature', type=float, default=None)
    ap.add_argument('--top-p', type=float, default=None)
    ap.add_argument('--top-k', type=int, default=None)
    ap.add_argument('--offset', type=int, default=0,
                    help='first manifest line to consider')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--files', default=None,
                    help='comma-separated file_name list: regenerate exactly '
                         'these items, ignoring state and existing mp3')
    ap.add_argument('--timeout', type=int, default=600,
                    help='per-request timeout, seconds')
    ap.add_argument('--retries', type=int, default=3)
    ap.add_argument('--retry-wait', type=float, default=5.0)
    args = ap.parse_args()

    ffmpeg = find_ffmpeg()
    url = args.url.rstrip('/') + '/v1/audio/speech'
    state_path = args.state or (args.manifest + '.audiocpp.state.jsonl')
    done = load_state(state_path)

    with open(args.manifest, encoding='utf-8') as f:
        manifest = [json.loads(line) for line in f if line.strip()]

    if args.files:
        wanted = {name.strip() for name in args.files.split(',') if name.strip()}
        todo = [m for m in manifest if m['file_name'] in wanted]
        missing = wanted - {m['file_name'] for m in todo}
        if missing:
            print(f"not in manifest: {', '.join(sorted(missing))}", file=sys.stderr)
        skipped = set()
        for m in todo:  # force regeneration
            dst = target_path(args.out, m)
            if os.path.exists(dst):
                try:
                    os.remove(dst)
                except PermissionError:
                    print(f"locked, skipping: {m['file_name']}", file=sys.stderr)
                    skipped.add(m['file_name'])
            done.pop(m['file_name'], None)
        if skipped:
            todo = [m for m in todo if m['file_name'] not in skipped]
    else:
        todo = [m for m in manifest[args.offset:]
                if m['file_name'] not in done
                and not os.path.exists(target_path(args.out, m))]
    if args.limit:
        todo = todo[:args.limit]
    print(f"manifest: {len(manifest)}, done: {len(done)}, todo: {len(todo)}")
    if not todo:
        return

    with open(args.ref, 'rb') as f:
        ref_b64 = base64.b64encode(f.read()).decode('ascii')
    ref_text = open(args.ref_text_file, encoding='utf-8').read().strip()

    session = requests.Session()
    total_done = total_err = 0
    audio_seconds = 0.0
    started = time.time()
    state_fh = open(state_path, 'a', encoding='utf-8')
    try:
        for i, item in enumerate(todo, 1):
            rec = synth_one(session, url, args.model, item, ref_b64,
                            ref_text, args, ffmpeg)
            state_fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
            state_fh.flush()
            if rec['status'] == 'done':
                total_done += 1
                audio_seconds += rec['duration']
                print(f"[{i}/{len(todo)}] {rec['file_name']}: "
                      f"{rec['duration']}s audio, {rec['elapsed_s']}s")
            else:
                total_err += 1
                print(f"[{i}/{len(todo)}] ERROR {rec['file_name']}: "
                      f"{rec.get('error')}", file=sys.stderr)
    finally:
        state_fh.close()

    wall = time.time() - started
    print(f"finished: {total_done} done, {total_err} errors, "
          f"{audio_seconds:.0f}s audio in {wall / 60:.1f} min "
          f"(rtf {audio_seconds / wall:.2f}x)" if wall else 'finished')


if __name__ == '__main__':
    main()
