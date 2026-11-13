"""Search and rank independently available subtitle providers."""
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from difflib import SequenceMatcher
from gzip import GzipFile
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path
import re
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from zipfile import ZipFile, BadZipFile

from opensubtitles import OpenSubtitles, SubtitleError, normalise, TIMECODE

LIMIT = 4 * 1024 * 1024
ADDIC = 'https://www.addic7ed.com'
POD = 'https://www.podnapisi.net'
SOURCE = 'https://api.subsource.net/api/v1'
NAMES = {'open': 'OpenSubtitles', 'addic': 'Addic7ed', 'pod': 'Podnapisi',
         'bs': 'BSPlayer', 'dl': 'SubDL', 'source': 'SubSource'}
RESOURCE = Path(__file__).resolve().parent/'resources'


@dataclass(frozen=True)
class Result:
    provider: str
    id: str
    language: str
    name: str
    hi: bool = False
    verified: bool = False
    sync: bool = False
    popularity: int = 0


def request(url, params=None, headers=None, secret=None, data=None, json_result=False):
    req = Request(url + ('?' + urlencode(params, doseq=True) if params else ''),
                  data=data, headers={'User-Agent': 'MDM Subs v0.2.0', **(headers or {})})
    if secret:
        req.add_unredirected_header(*secret)
    with urlopen(req, timeout=4) as response:
        body = response.read(LIMIT+1)
    if len(body) > LIMIT:
        raise SubtitleError('Subtitle provider response exceeded the size limit.')
    if json_result:
        value = json.loads(body)
        if not isinstance(value, dict) or value.get('error') or value.get('success') is False:
            raise SubtitleError('Subtitle provider rejected the request.')
        return value
    return body


def episode_marker(name):
    match = re.search(r'(?i)(?:\b|_)s(\d{1,2})[. _-]*e(\d{1,3})(?!\d)|\b(\d{1,2})x(\d{1,3})(?!\d)', name)
    if match:
        return tuple(int(value) for value in match.groups() if value is not None)
    return None


def matches_episode(name, target):
    marker = episode_marker(name)
    return not target or not marker or marker == target


def rank(rows, media):
    release = normalise(media['release'])
    preferred = media['preferred']
    languages = media['languages']
    episode = media['episode']
    unique = {}

    def score(row):
        name = normalise(row.name)
        similarity = SequenceMatcher(None, release, name, autojunk=False).ratio() if release and name else 0
        return (row.language == preferred, row.sync, row.verified or bool(episode and episode_marker(row.name) == episode),
                bool(release and name == release), similarity, not row.hi,
                min(row.popularity, 100), row.name, row.provider, row.id)

    for row in rows:
        if not isinstance(row.name, str) or not isinstance(row.language, str) or not row.language:
            continue
        if languages and row.language not in languages:
            continue
        if not matches_episode(row.name, episode):
            continue
        if episode and not (row.verified or row.sync or episode_marker(row.name) == episode):
            continue
        key = row.provider, row.id
        if key not in unique or score(row) > score(unique[key]):
            unique[key] = row
    return sorted(unique.values(), key=score, reverse=True)[:50]


class Providers:
    def __init__(self, setting, to_language, to_name, to_three):
        self.setting = setting
        self.to_language = to_language
        self.to_name = to_name
        self.to_three = to_three

    def search(self, media):
        enabled = [name for name in NAMES if self.setting('enable_'+name) != 'false']
        needed = {'open': 'api_key', 'dl': 'subdl_key', 'source': 'subsource_key'}
        enabled = [name for name in enabled if name not in needed or self.setting(needed[name]).strip()]
        if not enabled:
            raise SubtitleError('Enable a provider or enter its API key in MDM Subs settings.')
        executor = ThreadPoolExecutor(max_workers=len(enabled))
        tasks = {executor.submit(getattr(self, name), media): name for name in enabled}
        results, failures, completed = [], [], 0
        try:
            done, pending = wait(tasks, timeout=9)
            failures.extend(NAMES[tasks[task]] for task in pending)
            for task in done:
                try:
                    results.extend(task.result())
                    completed += 1
                except (SubtitleError, OSError, ValueError, TypeError, KeyError, AttributeError, ET.ParseError):
                    failures.append(NAMES[tasks[task]])
        finally:
            executor.shutdown(wait=False)
        if not completed:
            raise SubtitleError('Subtitle providers are unavailable. Check your connection and provider keys.')
        return rank(results, media), failures

    def open(self, media):
        client = OpenSubtitles(self.setting('api_key'))
        query = media['query'].copy()
        if media['hash']:
            query['moviehash'] = media['hash']
        return [Result('open', str(file_id), lang, name, hi,
                       verified=bool(media['episode']), sync=matched, popularity=score[-1])
                for score, file_id, lang, name, hi, matched in
                client.search(query, media['preferred'], normalise(media['release']), include_sync=True)]

    def dl(self, media):
        name, episode = media['title'], media['episode']
        if not (name or media['imdb'] or media['tmdb']):
            return []
        params = {'type': 'tv' if episode else 'movie', 'languages': ','.join(media['languages']),
                  'film_name': name, 'file_name': media['release']}
        if media['year']:
            params['year'] = media['year']
        if media['imdb'] and not episode:
            params['imdb_id'] = media['imdb']
        elif media['tmdb']:
            params['tmdb_id'] = media['tmdb']
        if episode:
            params['season'], params['episode'] = episode
        data = request('https://api.subdl.com/api/v2/subtitles/search', params,
                       secret=('Authorization', 'Bearer '+self.setting('subdl_key').strip()), json_result=True)
        return [Result('dl', r['url'], r['language'].lower(), r['release_name'],
                       bool(r.get('hi')), bool(episode))
                for r in data.get('subtitles', []) if isinstance(r, dict)
                and all(isinstance(r.get(key), str) and r[key] for key in ('url','language','release_name'))]

    def pod(self, media):
        if not media['title']:
            return []
        episode = media['episode']
        params = {'keywords': media['title'], 'language': media['languages'],
                  'movie_type': ['tv-series', 'mini-series'] if episode else 'movie'}
        if media['year']:
            params['year'] = media['year']
        if episode:
            params['seasons'], params['episodes'] = episode
        data = request(POD+'/subtitles/search/advanced', params,
                       headers={'Accept': 'application/json'}, json_result=True)
        results = []
        for row in data.get('data', []):
            if not isinstance(row, dict) or not row.get('publish_id'):
                continue
            for name in (row.get('custom_releases') or [media['title']])[:20]:
                results.append(Result('pod', str(row['publish_id']), row.get('language', ''),
                                      name, 'hearing_impaired' in (row.get('flags') or []), bool(episode)))
        return results

    def source(self, media):
        episode = media['episode']
        if not (media['title'] or media['imdb']):
            return []
        auth = ('X-API-Key', self.setting('subsource_key').strip())
        movie_type = 'tvseries' if episode else 'movie'
        params = {'searchType': 'text', 'q': media['title'], 'type': movie_type}
        if media['year']:
            params['year'] = media['year']
        if media['imdb']:
            params = {'searchType': 'imdb', 'imdb': media['imdb']}
        data = request(SOURCE+'/movies/search', params, secret=auth, json_result=True)
        rows = data.get('data', [])
        if episode:
            rows = [row for row in rows if str(row.get('season')) == str(episode[0])]
        if media['imdb']:
            rows = [row for row in rows if row.get('imdbId', media['imdb']) == media['imdb']]
        if not rows or not rows[0].get('movieId'):
            return []
        names = {self.to_name(code).lower().replace(' ', '_'): code for code in media['languages']}
        if 'en' in media['languages']:
            names['english'] = 'en'
        if 'pt-br' in media['languages']:
            names['brazilian_portuguese'] = 'pt-br'
        if 'es' in media['languages']:
            names.update({'spanish_latin_america': 'es', 'spanish_spain': 'es'})
        data = request(SOURCE+'/subtitles', {'movieId': rows[0]['movieId'], 'language': ','.join(names)},
                       secret=auth, json_result=True)
        results = []
        for row in data.get('data', []):
            link = row.get('link', '')
            subtitle_id = urlsplit(link).path.rstrip('/').rsplit('/', 1)[-1] or row.get('subtitleId')
            lang = row.get('language', '')
            language = names.get(lang, self.to_language(lang.replace('_',' ')))
            for name in (row.get('releaseInfo') or [])[:20]:
                if subtitle_id and (not episode or episode_marker(name) == episode):
                    results.append(Result('source', str(subtitle_id), language, name,
                                          bool(row.get('hearingImpaired')), bool(episode)))
        return results

    def addic(self, media):
        if not media['episode'] or not media['title']:
            return []
        titles = json.loads((RESOURCE/'addic7ed-shows.json').read_text('utf-8'))
        languages = json.loads((RESOURCE/'addic7ed-languages.json').read_text('utf-8'))
        title = media['title']
        lookup = {name.casefold(): value for name, value in titles.items()}
        show = lookup.get(f"{title} ({media['year']})".casefold()) or lookup.get(title.casefold())
        if not show:
            pattern = re.escape(title.casefold()) + r' \(\d{4}\)'
            alternatives = {value for name, value in lookup.items() if re.fullmatch(pattern, name)}
            if len(alternatives) == 1:
                show = alternatives.pop()
        if not show:
            return []
        season, episode = media['episode']
        ids = [str(value) for label, value in languages.items()
               if self.to_language(label) in media['languages']]
        params = {'show': show, 'season': season}
        if ids:
            params['langs'] = '|'+'|'.join(ids)+'|'
        parser = AddicTable()
        parser.feed(request(ADDIC+'/ajax_loadShow.php', params).decode('utf-8', errors='replace'))
        results = []
        for cells, links in parser.rows:
            if len(cells) < 10 or not links or not all(v.isdecimal() for v in cells[:2]):
                continue
            if tuple(map(int, cells[:2])) != (season, episode) or 'completed' not in cells[5].casefold():
                continue
            href = next((link for link in links if re.fullmatch(r'/(?:updated|original)/[\d/]+', link)), '')
            if href:
                results.append(Result('addic', href, self.to_language(cells[3]),
                                      f'{title}.S{season:02}E{episode:02}.{cells[4]}', bool(cells[6]), True))
        return results

    def bs(self, media):
        if not (media['imdb'] or media['episode_imdb'] or media['hash']):
            return []
        languages = {self.to_three(code): code for code in media['languages']}
        imdb = media['imdb'][2:] if media['imdb'] else ''
        if media['episode']:
            imdb = ''
        if media['episode_imdb']:
            imdb = media['episode_imdb'][2:]
        if not (imdb or media['hash']):
            return []
        url = 'http://s1.api.bsplayer-subtitles.com/v1.php'

        def soap(action, fields):
            envelope = ET.Element('{http://schemas.xmlsoap.org/soap/envelope/}Envelope')
            body = ET.SubElement(envelope, '{http://schemas.xmlsoap.org/soap/envelope/}Body')
            method = ET.SubElement(body, f'{{{url}}}{action}')
            for key, value in fields.items():
                ET.SubElement(method, key).text = str(value)
            data = request(url, data=ET.tostring(envelope), headers={
                'Content-Type': 'text/xml; charset=utf-8', 'User-Agent': 'BSPlayer/2.x (1022.12362)',
                'SOAPAction': f'"{url}#{action}"'})
            response = ET.fromstring(data)
            for node in response.iter():
                node.tag = node.tag.rsplit('}', 1)[-1]
            result = response.find('.//return')
            if result is None:
                raise SubtitleError('BSPlayer returned an unreadable response.')
            return result

        auth = soap('logIn', {'username': '', 'password': '', 'AppID': 'BSPlayer v2.72'})
        token = auth.findtext('data')
        if auth.findtext('result') != '200' or not token:
            raise SubtitleError('BSPlayer search could not start.')
        try:
            result = soap('searchSubtitles', {'handle': token, 'movieHash': media['hash'] or '0',
                          'movieSize': media['size'] or '0', 'languageId': ','.join(filter(None, languages)),
                          'imdbId': imdb})
        finally:
            try:
                soap('logOut', {'handle': token})
            except (OSError, ValueError, ET.ParseError, SubtitleError):
                pass
        results = []
        for row in result.findall('data/item'):
            url = row.findtext('subDownloadLink')
            name = row.findtext('subName')
            lang = row.findtext('subLang', '')
            if url and name:
                results.append(Result('bs', url, languages.get(lang) or self.to_language(lang), name,
                                      verified=bool(media['episode_imdb']), sync=bool(media['hash'] and not imdb)))
        return results

    def download(self, row, episode, release):
        provider, identifier = row['provider'], row['id']
        if provider == 'open' and identifier.isdecimal():
            return OpenSubtitles(self.setting('api_key')).download(
                int(identifier), self.setting('open_username'), self.setting('open_password'))
        if provider == 'pod' and re.fullmatch(r'[\w-]{1,80}', identifier):
            data = request(POD+'/subtitles/'+identifier+'/download')
        elif provider == 'addic' and re.fullmatch(r'/(?:updated|original)/[\d/]+', identifier):
            data = request(ADDIC+identifier, headers={'Referer': ADDIC+'/'})
        elif provider == 'source' and re.fullmatch(r'[\w-]{1,100}', identifier):
            data = request(SOURCE+'/subtitles/'+identifier+'/download',
                           secret=('X-API-Key', self.setting('subsource_key').strip()))
        elif provider == 'dl' and re.fullmatch(r'/[\w/%+.=-]+', identifier):
            data = request('https://dl.subdl.com'+identifier)
        elif provider == 'bs' and safe_bs_download(identifier):
            data = request(identifier)
        else:
            raise SubtitleError('Select a subtitle from the search results.')
        return subtitle_text(data, episode, release)


class AddicTable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = ([], [])
        elif self.row and tag == 'td':
            self.cell = []
        elif self.row and tag == 'a' and 'href' in dict(attrs):
            self.row[1].append(dict(attrs)['href'])

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if self.row and tag == 'td' and self.cell is not None:
            self.row[0].append(''.join(self.cell).strip())
            self.cell = None
        elif self.row and tag == 'tr':
            self.rows.append(self.row)
            self.row = self.cell = None


def safe_bs_download(url):
    address = urlsplit(url)
    return (address.scheme in ('http', 'https') and not address.username and
            bool(re.fullmatch(r'(?:[a-z\d-]+\.)*bsplayer-subtitles\.com', address.netloc)))


def subtitle_text(data, episode, release):
    if data.startswith(b'PK\x03\x04'):
        try:
            with ZipFile(BytesIO(data)) as archive:
                entries = [entry for entry in archive.infolist() if entry.filename.lower().endswith('.srt')
                           and entry.file_size <= LIMIT and matches_episode(entry.filename, episode)]
                if episode and len(entries) > 1:
                    entries = [entry for entry in entries if episode_marker(entry.filename) == episode]
                entries.sort(key=lambda entry: SequenceMatcher(
                    None, normalise(release), normalise(entry.filename.rsplit('/', 1)[-1])).ratio(), reverse=True)
                if not entries or (len(entries) > 1 and normalise(entries[0].filename.rsplit('/', 1)[-1]) != normalise(release)):
                    raise SubtitleError('The subtitle archive did not identify a matching SRT.')
                with archive.open(entries[0]) as subtitle:
                    data = subtitle.read(LIMIT+1)
        except (BadZipFile, RuntimeError) as error:
            raise SubtitleError('Could not read the subtitle archive.') from error
    elif data.startswith(b'\x1f\x8b'):
        with GzipFile(fileobj=BytesIO(data)) as subtitle:
            data = subtitle.read(LIMIT+1)
    if len(data) > LIMIT:
        raise SubtitleError('The subtitle exceeded the size limit.')
    try:
        encoding = 'utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
        text = data.decode(encoding)
    except UnicodeError:
        text = data.decode('cp1252', errors='replace')
    if not TIMECODE.search(text):
        raise SubtitleError('The download did not contain a readable SRT subtitle.')
    return text
