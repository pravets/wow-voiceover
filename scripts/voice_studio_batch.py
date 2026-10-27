"""Voice a JSONL manifest through Speech Studio's MCP server.

Speech Studio 0.2.4 crashes on this CPU when it runs its ASR engine on a cloned
voice's reference (transcribe_engine.dll, illegal instruction). Giving every
block its own reference (block_ref_set with whole=true and a ready transcript)
skips that path entirely.

Example:
    python scripts/voice_studio_batch.py --manifest samples/tauren-male/manifest.jsonl \
        --out AI_VoiceOverData_Vanilla/generated/sounds --limit 5
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Studio:
    def __init__(self, url: str):
        self.url = url
        self.session = requests.Session()
        self.next_id = 1
        self.session.post(self.url, json={
            'jsonrpc': '2.0', 'id': self.next_id, 'method': 'initialize',
            'params': {
                'protocolVersion': '2025-06-18',
                'capabilities': {},
                'clientInfo': {'name': 'wow-voiceover', 'version': '0.1'},
            },
        }, headers={'Accept': 'application/json, text/event-stream'}, timeout=30)

    def call(self, name: str, arguments: dict | None = None, timeout: int = 60):
        self.next_id += 1
        body = {
            'jsonrpc': '2.0', 'id': self.next_id, 'method': 'tools/call',
            'params': {'name': name, 'arguments': arguments or {}},
        }
        r = self.session.post(
            self.url, json=body,
            headers={'Accept': 'application/json, text/event-stream',
                     'Content-Type': 'application/json'},
            timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if 'error' in data:
            raise RuntimeError(f"{name}: {data['error']}")
        content = data.get('result', {}).get('content', [])
        text = ''.join(c.get('text', '') for c in content if c.get('type') == 'text')
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


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


def to_mp3(src: str, dst: str):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + '.tmp.mp3'
    subprocess.run(
        ['ffmpeg', '-y', '-v', 'error', '-i', src, '-c:a', 'libmp3lame',
         '-b:a', '64k', '-ar', '24000', '-ac', '1', tmp],
        check=True)
    os.replace(tmp, dst)


def extract_from_project(pid: str, items: list, projects_dir: str, out_dir: str,
                         state_path: str):
    project_path = os.path.join(projects_dir, pid, 'project.json')
    with open(project_path, encoding='utf-8') as fh:
        project = json.load(fh)
    blocks = project['chapters'][0]['blocks']
    records = []
    with open(state_path, 'a', encoding='utf-8') as state_fh:
        for i, block in enumerate(blocks):
            if i >= len(items):
                break
            item = items[i]
            takes = [t for t in (block.get('takes') or []) if t.get('source') == 'synth']
            if not takes:
                rec = {'file_name': item['file_name'], 'status': 'error',
                       'error': 'no take', 'pid': pid}
                state_fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
                records.append(rec)
                continue
            take = next((t for t in takes if t.get('n') == block.get('active')), takes[-1])
            src = os.path.join(projects_dir, pid, 'takes', take['file'])
            dst = target_path(out_dir, item)
            try:
                to_mp3(src, dst)
                rec = {'file_name': item['file_name'], 'status': 'done', 'pid': pid,
                       'duration': take.get('duration'),
                       'qc': (take.get('qc') or {}).get('passed'),
                       'chars': item['chars']}
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                rec = {'file_name': item['file_name'], 'status': 'error',
                       'error': f'convert: {e}', 'pid': pid}
            state_fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
            records.append(rec)
    return records


def synth_batch(studio: Studio, items: list, ref_path: str, ref_text: str,
                lang: str, engine: str, out_dir: str, state_path: str,
                projects_dir: str, batch_timeout: int):
    pid = studio.call('project_create', {
        'name': f"batch-{items[0]['file_name']}",
        'text': 'placeholder',
        'lang': lang,
        'engine': engine,
    })['project_id']

    project = studio.call('project_get', {'pid': pid})
    template = project['chapters'][0]['blocks'][0]
    blocks = []
    by_id = {}
    for i, item in enumerate(items):
        b = dict(template)
        b['id'] = hashlib.md5(f"{pid}:{i}".encode()).hexdigest()[:10]
        b['speaker'] = 'Рассказчик'
        b['text'] = item['text']
        b['voice'] = None
        b['direction'] = ''
        b['pause_ms'] = None
        b['hidden'] = False
        b['takes'] = []
        b['active'] = None
        b['pinned'] = None
        b['error'] = None
        b['note'] = ''
        b['marker'] = ''
        b['start'] = None
        b['end'] = None
        b.pop('own_ref', None)
        blocks.append(b)
        by_id[b['id']] = item

    project['chapters'][0]['blocks'] = blocks
    project['settings']['voice'] = None
    # The studio's QC can run speech recognition (transcribe_engine.dll), which
    # crashes on this CPU; the batch checks durations itself.
    qc = project['settings'].setdefault('qc', {})
    qc['asr_check'] = False
    qc['similarity'] = False
    qc['heuristics'] = []
    studio.call('project_update', {'pid': pid, 'project': project})

    for b in blocks:
        studio.call('block_ref_set', {
            'pid': pid, 'block': b['id'], 'path': ref_path,
            'text': ref_text, 'whole': True,
        })

    job = studio.call('project_synth', {'pid': pid})['job_id']
    started = time.time()
    while True:
        w = studio.call('studio_wait', {'job_id': job, 'seconds': 50}, timeout=90)
        if w.get('done'):
            break
        if time.time() - started > batch_timeout:
            raise TimeoutError(f"batch {pid} did not finish in {batch_timeout}s")

    records = extract_from_project(pid, items, projects_dir, out_dir, state_path)
    return pid, records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--manifest', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--state', default=None,
                    help='state file (default: <manifest>.state.jsonl)')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--batch-size', type=int, default=50)
    ap.add_argument('--lang', default='ru', help='Speech Studio language code, e.g. ru')
    ap.add_argument('--engine', default='higgs')
    ap.add_argument('--ref', required=True, help='reference WAV/MP3 for the voice')
    ap.add_argument('--ref-text-file', required=True,
                    help='text file with what is said in the reference')
    ap.add_argument('--url', default='http://127.0.0.1:8797/mcp')
    ap.add_argument('--batch-timeout', type=int, default=7200)
    ap.add_argument('--sleep-between-batches', type=float, default=2.0)
    ap.add_argument('--from-project', default=None,
                    help='extract takes of an existing project instead of voicing')
    ap.add_argument('--offset', type=int, default=0,
                    help='with --from-project: first manifest item of that project')
    ap.add_argument('--count', type=int, default=0,
                    help='with --from-project: how many manifest items it voiced')
    args = ap.parse_args()

    state_path = args.state or (args.manifest + '.state.jsonl')
    done = load_state(state_path)
    with open(args.manifest, encoding='utf-8') as f:
        manifest = [json.loads(line) for line in f if line.strip()]

    studio = Studio(args.url)
    projects_dir = studio.call('studio_paths', {})['projects_dir']

    if args.from_project:
        items = manifest[args.offset:args.offset + (args.count or None)]
        records = extract_from_project(args.from_project, items, projects_dir,
                                       args.out, state_path)
        ok = sum(1 for r in records if r['status'] == 'done')
        print(f"project {args.from_project}: {ok} done, {len(records) - ok} errors")
        return

    todo = [m for m in manifest if m['file_name'] not in done
            and not os.path.exists(target_path(args.out, m))]
    if args.limit:
        todo = todo[:args.limit]
    print(f"manifest: {len(manifest)}, done: {len(done)}, todo: {len(todo)}")

    ref_text = open(args.ref_text_file, encoding='utf-8').read().strip()

    total_done = total_err = 0
    for start in range(0, len(todo), args.batch_size):
        chunk = todo[start:start + args.batch_size]
        print(f"--- batch {start // args.batch_size + 1}: {len(chunk)} items")
        pid, records = synth_batch(studio, chunk, args.ref, ref_text, args.lang,
                                   args.engine, args.out, state_path,
                                   projects_dir, args.batch_timeout)
        ok = sum(1 for r in records if r['status'] == 'done')
        err = len(records) - ok
        total_done += ok
        total_err += err
        print(f"    project {pid}: {ok} done, {err} errors")
        if err:
            for r in records:
                if r['status'] != 'done':
                    print('    ERROR', r)
        time.sleep(args.sleep_between_batches)

    print(f"finished: {total_done} done, {total_err} errors")


if __name__ == '__main__':
    main()
