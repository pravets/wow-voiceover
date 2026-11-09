# VoiceOver for World of Warcraft

## v2: https://allvoice.ai
Contribute voices on [allvoice.ai](https://allvoice.ai) so I can give each NPC a unique AI voicemodel to power their dialog. The top rated voice for each NPC will be used. 


### [voiceover discord](https://discord.gg/VdhUmA8ZCt)
### [allvoice code](https://github.com/allvoice/allvoice-website)

## Overview
- tts cli to create audio files for quests and gossip text.
- in game addon for playing generated voiceovers

- cli uses data fetched from a local MySQL database and ElevenLabs tts for speech


## Below is for developers only. Go to [releases](https://github.com/mrthinger/wow-voiceover/releases) if youre looking to install the addon.

## Requirements
- python 3.10+
- docker (for the database)

## Installation
1. Make a python virtual environment. (make sure to source it after creating)
```bash
python -m venv .venv
```
2. Install the required packages.
```bash
pip install -r requirements.txt
```
3. Copy the .env.example file to .env and fill in your ElevenLabs API Key and database credentials. `MYSQL_DATABASE` must point at your AzerothCore world database (e.g. `acore_world`); the project's own tables are stored in a separate database (`MYSQL_VOICEOVER_DATABASE`, `wow_voiceover` by default).
```bash
cp .env.example .env
```
4. Create the project database and import the client display tables used to detect each NPC's race and gender:
```bash
python cli-main.py init-db
```
Your world database is not modified: the display tables live in the separate `wow_voiceover` database.

## Voice Setup
The generation scripts assume you have voices created in Elevenlabs named in the format `race-gender`. For the exact races the script checks your elevenlabs account for, refer to `tts_cli\consts.py`. Gender will always either be `male` or `female`. ex: `orc-male`. You will need to create your own voice clones. A good place to get samples is @ https://www.wowhead.com/sounds/npc-greetings/name:orc 
## Local Generation Pipeline (audio.cpp / Higgs)

Alternative to ElevenLabs: batch generation with a local audio.cpp server
(Higgs TTS with voice cloning, plus parakeet STT for reference transcripts).
All URLs are local infrastructure — pass them explicitly, no defaults.

Voice naming follows `race-gender` (see `RACE_DICT` in `tts_cli/consts.py`,
e.g. `orc-male`, `scourge-male`). Sources live in `voices/<voice>/*.ogg`,
working artifacts in `samples/<voice>/`.

### 1. Prepare a voice reference
1. Concat the sample oggs into one wav (24 kHz mono) at
   `samples/<voice>/ref.wav` (ffmpeg concat demuxer with a list file).
2. Transcribe it (parakeet via the server's `/v1/audio/transcriptions`,
   WAV only) into `samples/<voice>/ref.txt` — the reference text must match
   the audio.

### 2. Export the manifest
```bash
python scripts/export_voice_manifest.py --voice <voice> --out samples\<voice>\manifest.jsonl
```
Builds the JSONL from the database, filters by voice, drops progress rows,
keeps Cyrillic-only text for ruRU, dedups by text. Output files are named
`f-<quest>-accept.mp3` / `m-<quest>-accept.mp3` / `<quest>-complete.mp3`
under `quests/` and `<hash>.mp3` under `gossip/`.

### 3. Batch generation
```bash
python scripts\audiocpp_batch.py --manifest samples\<voice>\manifest.jsonl --out AI_VoiceOverData_Vanilla\generated\sounds --ref samples\<voice>\ref.wav --ref-text-file samples\<voice>\ref.txt --url <server> --model higgs-bf16 --max-tokens 8192
```
- `--url` is required; pass the base URL **without** `/v1`.
- Progress is tracked in `<manifest>.audiocpp.state.jsonl`; rerunning the
  same command resumes where it stopped.
- `--max-tokens 8192` avoids HTTP 500 on long text chunks; a file that still
  loops without finishing can be regenerated with `--files <name.mp3> --seed 7`.
- `--files` matches bare file names and forces regeneration (deletes the mp3
  and its state entry).

### 4. QA and review
```bash
python scripts\qa_server.py --url <server> --port 8765
python scripts\stt_batch.py --url <server>
```
`qa_server.py` serves a review UI on http://127.0.0.1:8765 (filter by
voice/NPC, verdict buttons, per-row speech recognition, regeneration from
the manifest text, reference sample playback). `stt_batch.py` batch-runs
speech recognition over all generated mp3 and writes a `<name>.json`
sidecar (`{voice, npc, text, recognized, verdict}`) next to each file;
the QA server merges those sidecars.

### Database restore from `backup/`
`docker compose up -d` (MySQL 8.4, container `mysql-server`), create the
databases (utf8mb4), then import each dump with
`docker exec -i mysql-server mysql -uroot -p... <db> < dump.sql`.
The server runs on Linux: table names are lowercase and case-sensitive.

## Usage
To use the interactive CLI tool, run the following command:

```bash
python cli-main.py
```

### Language Client Selection
Currently there are no voice translations available for languages other than english. However, if you want to use the addon with a non English client, you can still do so by creating the lookup tables in the client's respective language.

To create the lookup tables, you can use the following command, with `LANGUAGE_CODE` representing the required language for the client:
```bash
python cli-main.py gen_lookup_tables --lang=LANGUAGE_CODE
```
The default selection, when no language code is provided, is English. Please be aware that the quality of text completion for translations in languages other than English can vary significantly.

The following language codes are supported:
| Language Code | Language |
| ------------- | ------- |
| enUS          | English |
| enGB          | English |
| koKR          | Korean |
| frFR          | French |
| deDE          | German |
| zhCN          | Simplified Chinese |
| zhTW          | Traditional Chinese |
| esES          | European Spanish |
| esMX          | Mexican Spanish |
| ruRU          | Russian |

## Output
The generated TTS audio files will be saved in the sounds folder, with separate subfolders for quests and gossip. Lookup tables and sound length tables will also be generated for use in the addon. 

## Addon Install
Copy over the `generated` folder to the VoiceOverData_Vanilla folder, then the VoiceOver and VoiceOverData_Vanilla folder to `World of Warcraft/_classic_era_/Interface/AddOns`. Alternatively, you can syslink instead of copying for faster development.
Example syslink:
```bash
export WOW_DIR=PATH_OF_YOUR_WOW_DIR
ln -s ./VoiceOver "$WOW_DIR/_classic_era_/Interface/AddOns"
ln -s ./VoiceOver_Vanilla "$WOW_DIR/_classic_era_/Interface/AddOns"
```
## Contributing
If you want to contribute to this project, please feel free to open an issue or submit a pull request.

# CLI Docs

## Dataframe Schema

The dataframe schema before calling the `preprocess_dataframe` function consists of the following columns:

| Column        | Description                                                  |
|---------------|--------------------------------------------------------------|
| `source`      | Indicates the type of interaction, can be 'accept', 'progress', 'complete', or 'gossip' |
| `quest`       | The quest ID or empty string if it's a gossip interaction    |
| `text`        | The text template content of the interaction                           |
| `DisplayRaceID` | The race ID of the NPC involved in the interaction          |
| `DisplaySexID`  | The gender ID of the NPC involved in the interaction        |
| `name`        | The name of the NPC involved in the interaction               |
| `type`        | The type of the NPC involved in the interaction ('creature', 'gameobject', or 'item') |
| `id`          | The creature/gameobject/item ID of the NPC involved in the interaction |

`DisplayRaceID = -1` is used for interactions with inanimate NPCs: gameobjects, items etc. It's mapped to a voice called "narrator" in `RACE_DICT`.

## New Fields Added by `preprocess_dataframe`

The `preprocess_dataframe` function adds the following new fields to the dataframe:

| Column                   | Description                                                  |
|--------------------------|--------------------------------------------------------------|
| `race`                   | The race of the NPC, mapped from `DisplayRaceID` using `RACE_DICT` |
| `gender`                 | The gender of the NPC, mapped from `DisplaySexID` using `GENDER_DICT` |
| `voice_name`             | The voice name, which is a combination of the race and gender fields |
| `templateText_race_gender` | A combination of the text, race, and gender fields          |
| `templateText_race_gender_hash` | A hash of the `templateText_race_gender` field          |
| `cleanedText` | `text` after rendering template |
