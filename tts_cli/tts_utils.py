import requests
import os
import pandas as pd
from tqdm import tqdm
import hashlib
from concurrent.futures import ThreadPoolExecutor
import re
from tts_cli.env_vars import ELEVENLABS_API_KEY
from tts_cli.consts import RACE_DICT, GENDER_DICT
from tts_cli.length_table import write_sound_length_table_lua
from tts_cli.utils import get_first_n_words, get_last_n_words, replace_dollar_bs_with_space
from slpp import slpp as lua

# TODO: make module name a cli arg when we do other expansions
MODULE_NAME = 'AI_VoiceOverData_Vanilla'
OUTPUT_FOLDER = MODULE_NAME + '/generated'
SOUND_OUTPUT_FOLDER =  OUTPUT_FOLDER + '/sounds'
DATAMODULE_TABLE_GUARD_CLAUSE = 'if not VoiceOver or not VoiceOver.DataModules then return end'
REPLACE_DICT = {'$b': '\n', '$B': '\n', '$n': 'adventurer', '$N': 'Adventurer',
                '$C': 'Adventurer', '$c': 'adventurer', '$R': 'Traveler', '$r': 'traveler'}

# The Russian client wraps class/race placeholders in |3-6(...) / |1-3(...), e.g. |3-6($c).
GENDER_MARKUP_RE = re.compile(r'\|[0-9]+-\d+[()]([^)]*)\)?')
# |4(singular:few:many); picks the plural form for a number before it.
PLURAL_MARKUP_RE = re.compile(r'\|4\(([^)]*)\);?')
PLAYER_PLACEHOLDER_RE = re.compile(r'\$[NnCcRr]')
# $g male:female; (the Russian client adds a case marker after the female form);
# a few localized strings end the form with a comma instead of ';'
GENDER_OPTION_SEMI_RE = re.compile(r'\$[Gg]\s*([^:;]+?)\s*:\s*([^;]+?)\s*;')
GENDER_OPTION_COMMA_RE = re.compile(r'\$[Gg]\s*([^:;,.!?]+?)\s*:\s*([^:;,.!?]+?)\s*,')
CYRILLIC_RE = re.compile(r'[\u0400-\u04FF]')

# player-name/class/race placeholders, capitalized and lowercase forms
PLAYER_FORMS = {
    'ru': {'m': ('Путник', 'путник'), 'f': ('Путница', 'путница')},
    'en': {'m': ('Adventurer', 'adventurer'), 'f': ('Adventurer', 'adventurer')},
}
RACE_FORMS_EN = ('Traveler', 'traveler')

# Masculine predicate forms that must agree with a female player in the
# f-variants of Russian texts (the DB keeps one shared text). Word-boundary,
# case-preserving; extend as more mismatches surface.
FEMALE_FORMS = {
    'готов': 'готова',
}


def apply_female_agreement(text):
    def repl(m):
        word = m.group(0)
        female = FEMALE_FORMS[word.lower()]
        return female.capitalize() if word[0].isupper() else female
    pattern = re.compile(r'\b(' + '|'.join(FEMALE_FORMS) + r')\b',
                         re.IGNORECASE | re.UNICODE)
    return pattern.sub(repl, text)


def is_russian_text(text):
    return bool(CYRILLIC_RE.search(text))


def strip_gender_markup(text):
    return GENDER_MARKUP_RE.sub(lambda m: m.group(1), text)


def _starts_sentence(prefix):
    return not prefix.strip() or bool(re.search(r'[.!?…»"\')\]]\s*$', prefix))


def replace_player_placeholders(text, language, gender):
    def repl(m):
        token = m.group(0)
        if language == 'ru':
            cap, low = PLAYER_FORMS['ru'][gender]
        elif token.lower() == '$r':
            cap, low = RACE_FORMS_EN
        else:
            cap, low = PLAYER_FORMS['en'][gender]
        return cap if _starts_sentence(text[:m.start()]) else low
    return PLAYER_PLACEHOLDER_RE.sub(repl, text)


def apply_gender_option(text, gender):
    idx = 1 if gender == 'm' else 2

    def repl_semi(m):
        form = m.group(idx).strip()
        return re.sub(r'\s*:[^:]*$', '', form)  # drop the case marker

    def repl_comma(m):
        return m.group(idx) + ','

    text = GENDER_OPTION_SEMI_RE.sub(repl_semi, text)
    return GENDER_OPTION_COMMA_RE.sub(repl_comma, text)


def apply_plural_markup(text):
    def repl(m):
        forms = [f.strip() for f in m.group(1).split(':')]
        if not forms:
            return ''
        number = re.search(r'(\d+)\s*$', text[:m.start()])
        if number and len(forms) >= 3:
            n = int(number.group(1))
            if n % 10 == 1 and n % 100 != 11:
                idx = 0
            elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
                idx = 1
            else:
                idx = 2
            return forms[idx]
        return forms[0]
    return PLURAL_MARKUP_RE.sub(repl, text)


def build_cleaned_text(raw, language, gender):
    text = strip_gender_markup(raw)
    text = apply_plural_markup(text)
    text = text.replace('$b', '\n').replace('$B', '\n')
    text = re.sub(r'<.*?>\s', '', text)
    text = replace_player_placeholders(text, language, gender or 'm')
    if gender is not None:
        text = apply_gender_option(text, gender)
    if gender == 'f' and language == 'ru':
        text = apply_female_agreement(text)
    return text

def get_hash(text):
    hash_object = hashlib.md5(text.encode())
    return hash_object.hexdigest()

def create_output_subdirs(subdir: str):
    output_subdir = os.path.join(SOUND_OUTPUT_FOLDER, subdir)
    if not os.path.exists(output_subdir):
        os.makedirs(output_subdir)

def prune_quest_id_table(quest_id_table):
    def is_single_quest_id(nested_dict):
        if isinstance(nested_dict, dict):
            if len(nested_dict) == 1:
                return is_single_quest_id(next(iter(nested_dict.values())))
            else:
                return False
        else:
            return True

    def single_quest_id(nested_dict):
        if isinstance(nested_dict, dict):
            return single_quest_id(next(iter(nested_dict.values())))
        else:
            return nested_dict

    pruned_table = {}
    for source_key, source_value in quest_id_table.items():
        pruned_table[source_key] = {}
        for title_key, title_value in source_value.items():
            if is_single_quest_id(title_value):
                pruned_table[source_key][title_key] = single_quest_id(title_value)
            else:
                pruned_table[source_key][title_key] = {}
                for npc_key, npc_value in title_value.items():
                    if is_single_quest_id(npc_value):
                        pruned_table[source_key][title_key][npc_key] = single_quest_id(npc_value)
                    else:
                        pruned_table[source_key][title_key][npc_key] = npc_value

    return pruned_table

class TTSProcessor:
    def __init__(self):
        self.voice_map = self.fetch_voice_map()

    def get_voice_map(self):
        return self.voice_map

    def fetch_voice_map(self):
        url = "https://api.elevenlabs.io/v1/voices"
        headers = {
            "xi-api-key": ELEVENLABS_API_KEY
        }

        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            print("Error: unable to fetch data.")
            return {}

        try:
            response = response.json()
        except ValueError:
            print("Error: unable to fetch data.")
            return {}
        voice_map = {}

        for voice in response["voices"]:
            name_parts = voice["name"].split('-')
            if len(name_parts) == 2:
                race, gender = name_parts
                if race in RACE_DICT.values() and (gender == 'male' or gender == 'female'):
                    voice_map[voice["name"]] = voice["voice_id"]

        return voice_map

    def tts(self, text: str, voice: str, outputName: str, output_subfolder: str, forceGen: bool = False):
        result = ""
        outpath = os.path.join(SOUND_OUTPUT_FOLDER, output_subfolder, outputName)
        if os.path.isfile(outpath) and forceGen is not True:
            result = "duplicate generation, skipping"
            return

        voice_id = self.voice_map[voice]
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        payload = {
            "text": text,
            "voice_settings": {
                "stability": 0.28,
                "similarity_boost": .992
            }
        }
        headers = {
            "xi-api-key": ELEVENLABS_API_KEY
        }

        response = requests.post(url, json=payload, headers=headers)

        if response.status_code == 200 and response.headers["Content-Type"] == "audio/mpeg":
            with open(outpath, "wb") as f:
                f.write(response.content)
                result = f"Audio file saved successfully!: {outpath}"
                print(result)
        else:
            result = f"Error: unable to save audio file {response}"
            print(result)
        return result



    def handle_gender_options(self, text):
        pattern = re.compile(r'\$[Gg]\s*([^:;]+?)\s*:\s*([^:;]+?)\s*;')

        male_text = pattern.sub(r'\1', text)
        female_text = pattern.sub(r'\2', text)

        return male_text, female_text

    def preprocess_dataframe(self, df):
        df = df.copy() # prevent mutation on original df for safety
        df['race'] = df['DisplayRaceID'].map(RACE_DICT)
        df['gender'] = df['DisplaySexID'].map(GENDER_DICT)
        df['voice_name'] = df['race'] + '-' + df['gender']

        df['templateText_race_gender'] = df['original_text'] + df['race'] + df['gender']
        df['templateText_race_gender_hash'] = df['templateText_race_gender'].apply(get_hash)

        rows = []
        for _, row in df.iterrows():
            raw = str(row['text'])
            stripped = strip_gender_markup(raw)
            language = 'ru' if is_russian_text(stripped) else 'en'
            has_gender_option = bool(GENDER_OPTION_SEMI_RE.search(stripped)
                                     or GENDER_OPTION_COMMA_RE.search(stripped))
            has_player_placeholder = bool(PLAYER_PLACEHOLDER_RE.search(stripped))

            # Russian player placeholders need a male and a female take, like $G does:
            # the addon picks the m-/f- file by the player's gender.
            if has_gender_option or (language == 'ru' and has_player_placeholder):
                for tag in ('m', 'f'):
                    row_male = row.copy()
                    row_male['cleanedText'] = build_cleaned_text(raw, language, tag)
                    row_male['player_gender'] = tag
                    rows.append(row_male)
            else:
                row_copy = row.copy()
                row_copy['cleanedText'] = build_cleaned_text(raw, language, None)
                row_copy['player_gender'] = None
                rows.append(row_copy)

        new_df = pd.DataFrame(rows)
        new_df.reset_index(drop=True, inplace=True)

        return new_df


    def process_row(self, row_tuple):
        row = pd.Series(row_tuple[1:], index=row_tuple._fields[1:])
        voice_name = f'{row["race"]}-{row["gender"]}'
        custom_message = ""
        if "$" in row["cleanedText"] or "<" in row["cleanedText"] or ">" in row["cleanedText"]:
            custom_message = f'skipping due to invalid chars: {row["cleanedText"]}'
        elif voice_name not in self.selected_voice_names:
            custom_message = f'skipping due to voice being unselected or unavailable: {voice_name}'
        elif row['source'] == "progress": # skip progress text (progress text is usually better left unread since its always played before quest completion)
            custom_message = f'skipping progress text: {row["quest"]}-{row["source"]}'
        else:
            self.tts_row(row, voice_name)
        return custom_message

    def tts_row(self, row, voice_name):
        tts_text = row['cleanedText']
        file_name =  f'{row["quest"]}-{row["source"]}' if row['quest'] else f'{row["templateText_race_gender_hash"]}'
        if row['player_gender'] is not None:
            file_name = row['player_gender'] + '-'+ file_name
        file_name = file_name + '.mp3'
        subfolder = 'quests' if row['quest'] else 'gossip'
        self.tts(tts_text, voice_name, file_name, subfolder)

    def create_output_dirs(self):
        create_output_subdirs('')
        create_output_subdirs('quests')
        create_output_subdirs('gossip')

    def process_rows_in_parallel(self, df, row_proccesing_fn, selected_voice_names: list[str], max_workers=5):

        total_rows = len(df)
        bar_format = '{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}] {postfix}'
        self.selected_voice_names = set(selected_voice_names)

        with tqdm(total=total_rows, unit='rows', ncols=100, desc='Generating Audio', ascii=False, bar_format=bar_format, dynamic_ncols=True) as pbar:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                for row, custom_message in zip(df.iterrows(), executor.map(row_proccesing_fn, df.itertuples())):
                    pbar.set_postfix_str(custom_message)
                    pbar.update(1)


    def write_gossip_file_lookups_table(self, df, module_name, type, table, filename):
        output_file = OUTPUT_FOLDER + f"/{filename}.lua"
        gossip_table = {}

        accept_df = df[(df['quest'] == '') & (df['type'] == type)]

        for i, row in tqdm(accept_df.iterrows()):
            if row['id'] not in gossip_table:
                gossip_table[row['id']] = {}

            escapedText = row['text'].replace('"', '\'').replace('\r',' ').replace('\n',' ')

            gossip_table[row['id']][escapedText] = row['templateText_race_gender_hash']

        with open(output_file, "w", encoding="UTF-8") as f:
            f.write(DATAMODULE_TABLE_GUARD_CLAUSE + "\n")
            f.write(f"{module_name}.{table} = ")
            f.write(lua.encode(gossip_table))
            f.write("\n")

        print(f"Finished writing {filename}.lua")


    def write_questlog_npc_lookups_table(self, df, module_name, type, table, filename):
        output_file = OUTPUT_FOLDER + f"/{filename}.lua"
        questlog_table = {}

        accept_df = df[(df['source'] == 'accept') & (df['type'] == type)]

        for i, row in tqdm(accept_df.iterrows()):
            questlog_table[int(row['quest'])] = row['id']

        with open(output_file, "w", encoding="UTF-8") as f:
            f.write(DATAMODULE_TABLE_GUARD_CLAUSE + "\n")
            f.write(f"{module_name}.{table} = ")
            f.write(lua.encode(questlog_table))
            f.write("\n")

        print(f"Finished writing {filename}.lua")

    def write_npc_name_lookup_table(self, df, module_name, type, table, filename):
        output_file = OUTPUT_FOLDER + f"/{filename}.lua"
        npc_name_table = {}

        accept_df = df[df['type'] == type]

        for i, row in tqdm(accept_df.iterrows()):
            npc_name_table[row['id']] =  row['name']

        with open(output_file, "w", encoding="UTF-8") as f:
            f.write(DATAMODULE_TABLE_GUARD_CLAUSE + "\n")
            f.write(f"{module_name}.{table} = ")
            f.write(lua.encode(npc_name_table))
            f.write("\n")

        print(f"Finished writing {filename}.lua")

    def write_quest_id_lookup(self, df, module_name):
        output_file = OUTPUT_FOLDER + "/quest_id_lookups.lua"
        quest_id_table = {}

        quest_df = df[df['quest'] != '']

        for i, row in tqdm(quest_df.iterrows()):
            quest_source = row['source']
            if quest_source == 'progress': # skipping progress text for now
                continue

            quest_id = int(row['quest'])
            quest_title = row['quest_title']
            quest_text = get_first_n_words(row['text'], 15) + ' ' +  get_last_n_words(row['text'], 15)
            escaped_quest_text = replace_dollar_bs_with_space(quest_text.replace('"', '\'').replace('\r',' ').replace('\n',' '))
            escaped_quest_title = quest_title.replace('"', '\'').replace('\r',' ').replace('\n',' ')
            npc_name = row['name']
            escaped_npc_name = npc_name.replace('"', '\'').replace('\r',' ').replace('\n',' ')

            # table[source][title][npcName][text]
            if quest_source not in quest_id_table:
                quest_id_table[quest_source] = {}

            if escaped_quest_title not in quest_id_table[quest_source]:
                quest_id_table[quest_source][escaped_quest_title] = {}

            if escaped_npc_name not in quest_id_table[quest_source][escaped_quest_title]:
                quest_id_table[quest_source][escaped_quest_title][escaped_npc_name] = {}

            if quest_text not in quest_id_table[quest_source][escaped_quest_title][escaped_npc_name]:
                quest_id_table[quest_source][escaped_quest_title][escaped_npc_name][escaped_quest_text] = quest_id

        pruned_quest_id_table = prune_quest_id_table(quest_id_table)

        # UTF-8 Encoding is important for other languages!
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(DATAMODULE_TABLE_GUARD_CLAUSE + "\n")
            f.write(f"{module_name}.QuestIDLookup = ")
            f.write(lua.encode(pruned_quest_id_table))
            f.write("\n")

    def write_npc_name_gossip_file_lookups_table(self, df, module_name, type, table, filename):
        output_file = OUTPUT_FOLDER + f"/{filename}.lua"
        gossip_table = {}

        accept_df = df[(df['quest'] == '') & (df['type'] == type)]

        for i, row in tqdm(accept_df.iterrows()):
            npc_name = row['name']
            escaped_npc_name = npc_name.replace('"', '\'').replace('\r',' ').replace('\n',' ')

            if escaped_npc_name not in gossip_table:
                gossip_table[escaped_npc_name] = {}

            escapedText = row['text'].replace('"', '\'').replace('\r',' ').replace('\n',' ')

            gossip_table[escaped_npc_name][escapedText] = row['templateText_race_gender_hash']

        with open(output_file, "w", encoding="UTF-8") as f:
            f.write(DATAMODULE_TABLE_GUARD_CLAUSE + "\n")
            f.write(f"{module_name}.{table} = ")
            f.write(lua.encode(gossip_table))
            f.write("\n")

        print(f"Finished writing {filename}.lua")



    def tts_dataframe(self, df, selected_voices):
        self.create_output_dirs()
        self.process_rows_in_parallel(df, self.process_row, selected_voices, max_workers=5)
        print("Audio finished generating.")

    def generate_lookup_tables(self, df):
        self.create_output_dirs()
        self.write_gossip_file_lookups_table(df, MODULE_NAME, 'creature',   'GossipLookupByNPCID',    'npc_gossip_file_lookups')
        self.write_gossip_file_lookups_table(df, MODULE_NAME, 'gameobject', 'GossipLookupByObjectID', 'object_gossip_file_lookups')

        self.write_quest_id_lookup(df, MODULE_NAME)
        print("Finished writing quest_id_lookups.lua")

        self.write_npc_name_gossip_file_lookups_table(df, MODULE_NAME, 'creature',   'GossipLookupByNPCName',    'npc_name_gossip_file_lookups')
        self.write_npc_name_gossip_file_lookups_table(df, MODULE_NAME, 'gameobject', 'GossipLookupByObjectName', 'object_name_gossip_file_lookups')

        self.write_questlog_npc_lookups_table(df, MODULE_NAME, 'creature',   'NPCIDLookupByQuestID',    'questlog_npc_lookups')
        self.write_questlog_npc_lookups_table(df, MODULE_NAME, 'gameobject', 'ObjectIDLookupByQuestID', 'questlog_object_lookups')
        self.write_questlog_npc_lookups_table(df, MODULE_NAME, 'item',       'ItemIDLookupByQuestID',   'questlog_item_lookups')

        self.write_npc_name_lookup_table(df, MODULE_NAME, 'creature',   'NPCNameLookupByNPCID',       'npc_name_lookups')
        self.write_npc_name_lookup_table(df, MODULE_NAME, 'gameobject', 'ObjectNameLookupByObjectID', 'object_name_lookups')
        self.write_npc_name_lookup_table(df, MODULE_NAME, 'item',       'ItemNameLookupByItemID',     'item_name_lookups')

        write_sound_length_table_lua(MODULE_NAME, SOUND_OUTPUT_FOLDER, OUTPUT_FOLDER)
        print("Updated sound_length_table.lua")
