"""Export a voice's texts from the AzerothCore database into a JSONL manifest.

Example:
    python scripts/export_voice_manifest.py --voice tauren-male --lang ruRU \
        --out samples/tauren-male/manifest.jsonl
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tts_cli.sql_queries import query_dataframe_for_all_quests_and_gossip  # noqa: E402
from tts_cli.tts_utils import TTSProcessor  # noqa: E402


def build_manifest(voice: str, lang: str, allow_non_cyrillic: bool = False):
    df = query_dataframe_for_all_quests_and_gossip(lang)
    proc = TTSProcessor.__new__(TTSProcessor)  # no ElevenLabs call
    d = proc.preprocess_dataframe(df)

    d = d[d['voice_name'] == voice]
    d = d[d['source'] != 'progress']
    if lang not in ('enUS', 'enGB') and not allow_non_cyrillic:
        # rows without a translation fall back to the English text; they would be
        # voiced in English inside a localized data module
        d = d[d['text'].str.contains(r'[\u0400-\u04FF]', regex=True)]
    d = d.drop_duplicates(subset=['cleanedText', 'DisplayRaceID', 'DisplaySexID'])
    d = d[~d['cleanedText'].str.contains(r'[$<>]', regex=True)]
    d = d[d['cleanedText'].str.strip() != '']

    items = []
    for _, r in d.iterrows():
        is_quest = str(r['quest']) != ''
        if is_quest:
            file_name = f"{int(r['quest'])}-{r['source']}"
            if r['player_gender'] is not None:
                file_name = f"{r['player_gender']}-{file_name}"
            subfolder = 'quests'
        else:
            file_name = r['templateText_race_gender_hash']
            subfolder = 'gossip'
        items.append({
            'file_name': file_name + '.mp3',
            'subfolder': subfolder,
            'text': r['cleanedText'],
            'original_text': r['original_text'],
            'source': r['source'],
            'quest': int(r['quest']) if is_quest else None,
            'npc_id': int(r['id']),
            'npc_name': r['name'],
            'voice_name': r['voice_name'],
            'chars': len(r['cleanedText']),
        })

    # unique file names, deterministic order
    seen = set()
    unique = []
    for it in items:
        if it['file_name'] in seen:
            continue
        seen.add(it['file_name'])
        unique.append(it)

    quests = sorted([i for i in unique if i['subfolder'] == 'quests'],
                    key=lambda i: (i['quest'], i['source'], i['file_name']))
    gossip = sorted([i for i in unique if i['subfolder'] == 'gossip'],
                    key=lambda i: i['file_name'])

    # round-robin so a --limit run gets both kinds
    mixed = []
    while quests or gossip:
        if quests:
            mixed.append(quests.pop(0))
        if gossip:
            mixed.append(gossip.pop(0))
    return mixed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--voice', required=True)
    ap.add_argument('--lang', default='ruRU')
    ap.add_argument('--out', required=True)
    ap.add_argument('--allow-non-cyrillic', action='store_true',
                    help='keep English fallback rows in a localized module')
    args = ap.parse_args()

    items = build_manifest(args.voice, args.lang, args.allow_non_cyrillic)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + '\n')

    quests = sum(1 for i in items if i['subfolder'] == 'quests')
    gossip = sum(1 for i in items if i['subfolder'] == 'gossip')
    chars = sum(i['chars'] for i in items)
    print(f"manifest: {len(items)} items ({quests} quests, {gossip} gossip), "
          f"{chars} chars -> {args.out}")


if __name__ == '__main__':
    main()
