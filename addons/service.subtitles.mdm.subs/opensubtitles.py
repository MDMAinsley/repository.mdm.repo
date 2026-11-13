"""OpenSubtitles search and SRT downloads."""
import json
import re
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


API = 'https://api.opensubtitles.com/api/v1'
USER_AGENT = 'MDM Subs v0.2.0'
LIMIT = 2 * 1024 * 1024
TIMECODE = re.compile(r'^\d{2,}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2,}:\d{2}:\d{2}[,.]\d{3}', re.M)


class SubtitleError(Exception):
    pass


def read(request):
    try:
        with urlopen(request, timeout=8) as response:
            data = response.read(LIMIT + 1)
    except HTTPError as error:
        if error.code in (401, 403):
            raise SubtitleError('OpenSubtitles denied the request. Check your API key or download allowance.') from None
        if error.code == 429:
            raise SubtitleError('OpenSubtitles is limiting requests. Try again shortly.') from None
        raise SubtitleError('OpenSubtitles could not complete the request.') from None
    except (OSError, ValueError):
        raise SubtitleError('OpenSubtitles is unavailable. Try again shortly.') from None
    if len(data) > LIMIT:
        raise SubtitleError('The OpenSubtitles response was too large.')
    return data


class OpenSubtitles:
    def __init__(self, key):
        self.key = key.strip()
        self.api = API
        self.token = ''
        if not self.key:
            raise SubtitleError('Enter your OpenSubtitles API key in MDM Subs settings.')

    def request(self, path, query=None, body=None):
        request = Request(self.api + path + ('?' + urlencode(sorted(query.items())) if query else ''),
                          data=json.dumps(body).encode() if body else None,
                          headers={'User-Agent': USER_AGENT, 'Accept': 'application/json',
                                   'Content-Type': 'application/json'})
        request.add_unredirected_header('Api-Key', self.key)
        if self.token:
            request.add_unredirected_header('Authorization', 'Bearer '+self.token)
        try:
            result = json.loads(read(request))
        except (ValueError, UnicodeError):
            raise SubtitleError('OpenSubtitles returned an unreadable response.') from None
        if not isinstance(result, dict):
            raise SubtitleError('OpenSubtitles returned an unreadable response.')
        return result

    def search(self, query, preferred, release, *, include_sync=False):
        rows = self.request('/subtitles', query=query).get('data', [])
        if not isinstance(rows, list):
            raise SubtitleError('OpenSubtitles returned an unreadable search result.')
        matches = {}
        for row in rows:
            attributes = row.get('attributes') if isinstance(row, dict) else None
            if not isinstance(attributes, dict):
                continue
            feature = attributes.get('feature_details')
            if isinstance(feature, dict) and any(
                feature.get(field) is not None and str(feature[field]) != str(query[field])
                for field in ('season_number', 'episode_number', 'parent_tmdb_id', 'tmdb_id', 'imdb_id')
                if field in query
            ):
                continue
            language = attributes.get('language')
            files = attributes.get('files')
            if not isinstance(language, str) or not re.fullmatch(r'[a-z]{2,3}(?:-[a-z]{2,3})?', language):
                continue
            if query.get('languages') and language not in query['languages'].split(','):
                continue
            if not isinstance(files, list):
                continue
            for file in files:
                if not isinstance(file, dict) or type(file.get('file_id')) is not int or file['file_id'] <= 0:
                    continue
                name = file.get('file_name') or attributes.get('release')
                if not isinstance(name, str) or not name:
                    name = 'Subtitle'
                name = ' '.join(name.split())[:180]
                same_release = bool(release and normalise(name) == release)
                count = attributes.get('download_count')
                matched = bool(query.get('moviehash') and attributes.get('moviehash_match') is True)
                score = (language == preferred, matched, same_release, count if type(count) is int else 0)
                file_id = file['file_id']
                if file_id not in matches or score > matches[file_id][0]:
                    matches[file_id] = (score, file_id, language, name, attributes.get('hearing_impaired') is True, matched)
        rows = sorted(matches.values(), reverse=True)[:50]
        return rows if include_sync else [row[:-1] for row in rows]

    def download(self, file_id, username='', password=''):
        if username and password:
            login = self.request('/login', body={'username': username, 'password': password})
            self.token = login.get('token', '')
            if not isinstance(self.token, str) or not self.token:
                raise SubtitleError('OpenSubtitles sign-in failed. Check your username and password.')
            host = login.get('base_url', '')
            if isinstance(host, str) and re.fullmatch(r'[a-z0-9-]+\.opensubtitles\.com', host):
                self.api = 'https://'+host+'/api/v1'
        result = self.request('/download', body={'file_id': file_id, 'sub_format': 'srt'})
        link = result.get('link')
        if not isinstance(link, str) or urlsplit(link).scheme != 'https' or not urlsplit(link).hostname:
            raise SubtitleError('OpenSubtitles did not return a secure download link.')
        data = read(Request(link, headers={'User-Agent': USER_AGENT}))
        try:
            content = data.decode('utf-8-sig')
        except UnicodeError:
            content = data.decode('cp1252', errors='replace')
        if not TIMECODE.search(content):
            raise SubtitleError('The download did not contain a readable SRT subtitle.')
        return content


def normalise(name):
    stem = re.sub(r'\.(?:mkv|mp4|avi|srt|ass|vtt)$', '', name.casefold())
    return re.sub(r'[^\w]+', '', stem)
