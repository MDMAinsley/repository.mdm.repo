"""Kodi subtitle search and download entry point."""
import json
import ntpath
from pathlib import Path
import re
import sys
from struct import unpack
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from uuid import uuid4

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from opensubtitles import SubtitleError, normalise
from providers import NAMES, Providers


def number(value, minimum=1):
    if re.fullmatch(r'[0-9]{1,10}', str(value)) and int(value) >= minimum:
        return int(value)
    return None


def search_identity(params, info, flix):
    manual = params.get('searchstring', '').strip()
    if manual:
        return {'query': manual[:200].lower()}
    if flix.get('media_type') == 'episode' or info.getTVShowTitle():
        season = number(flix.get('season', info.getSeason()), 0)
        episode = number(flix.get('episode', info.getEpisode()))
        series = flix.get('tvshowtitle') or info.getTVShowTitle()
        series_id = number(flix.get('tmdb_id')) if flix.get('media_type') == 'episode' else None
        if season is None or episode is None or not (series_id or series):
            raise SubtitleError('Episode details are missing. Try KodiÃ¢â‚¬â„¢s manual subtitle search.')
        title = {'parent_tmdb_id': series_id} if series_id else {'query': series[:200].lower()}
        return dict(title, season_number=season, episode_number=episode)
    movie_id = number(flix.get('tmdb_id')) if flix.get('media_type') == 'movie' else None
    imdb = re.fullmatch(r'tt([0-9]{5,10})', info.getUniqueID('imdb') or '')
    if movie_id:
        return {'tmdb_id': movie_id}
    if imdb:
        return {'imdb_id': int(imdb[1])}
    title = flix.get('title') or info.getTitle()
    if not title:
        raise SubtitleError('Film details are missing. Try KodiÃ¢â‚¬â„¢s manual subtitle search.')
    year = number(flix.get('year', info.getYear()))
    query = {'query': title[:200].lower()}
    if year:
        query['year'] = year
    return query


def flix_identity(item):
    raw = item.getProperty('mdm.flix.progress')
    if not raw or len(raw) > 8192:
        return {}
    try:
        record = json.loads(raw)
    except ValueError:
        return {}
    if isinstance(record, dict) and record.get('schema') == 1 and isinstance(record.get('item'), dict):
        return record['item']
    return {}


def language_code(label):
    label = label.strip().lower()
    if not label:
        return ''
    if label in ('pob', 'pt-br', 'portuguese (brazil)', 'portuguese (brazilian)'):
        return 'pt-br'
    return xbmc.convertLanguage(label, xbmc.ISO_639_1)


def local_hash(path):
    parsed = urlsplit(path)
    if parsed.scheme not in ('', 'file') and not re.match(r'^[a-zA-Z]:[\\/]', path):
        return '', 0
    if parsed.scheme == 'file':
        if parsed.netloc not in ('', 'localhost'):
            return '', 0
        path = unquote(parsed.path)
        if re.match(r'^/[a-zA-Z]:', path):
            path = path[1:]
    if path.startswith(('//', '\\\\')):
        return '', 0
    try:
        with Path(path).open('rb') as source:
            size = source.seek(0, 2)
            if size < 131072:
                return '', 0
            source.seek(0)
            first = source.read(65536)
            source.seek(-65536, 2)
            last = source.read(65536)
            if len(first) != 65536 or len(last) != 65536:
                return '', 0
            value = (size + sum(unpack('<8192Q', first)) + sum(unpack('<8192Q', last))) & ((1 << 64)-1)
            return f'{value:016x}', size
    except (OSError, ValueError):
        return '', 0


def make_media(player, params):
    item = player.getPlayingItem()
    info = item.getVideoInfoTag()
    flix = flix_identity(item)
    query = search_identity(params, info, flix)
    preferred = language_code(params.get('preferredlanguage', ''))
    languages = list(dict.fromkeys(code for label in params.get('languages', '').split(',')
                                   if (code := language_code(label))))
    if preferred and preferred not in languages:
        languages.insert(0, preferred)
    if languages:
        query['languages'] = ','.join(languages)
    path = player.getPlayingFile().split('|', 1)[0]
    release = ntpath.basename(unquote(urlsplit(path).path))[:240]
    episode = ((query['season_number'], query['episode_number'])
               if 'episode_number' in query else None)
    manual = bool(params.get('searchstring', '').strip())
    digest, size = ('', 0) if manual else local_hash(path)
    tag_imdb = info.getUniqueID('imdb') or ''
    episode_imdb = tag_imdb if not flix or not episode else ''
    imdb = flix.get('imdb_id') or (tag_imdb if flix or not episode else '')
    title = (params['searchstring'].strip() if manual else
             ((flix.get('tvshowtitle') or info.getTVShowTitle()) if episode else
              (flix.get('title') or info.getTitle())))
    return dict(query=query, languages=languages, preferred=preferred, release=release,
                hash=digest, size=size, episode=episode, title=(title or '')[:200],
                year=None if manual else number(flix.get('year') or (info.getYear() if not episode else 0)),
                imdb=imdb if not manual and isinstance(imdb, str) and re.fullmatch(r'tt[0-9]{5,10}', imdb) else '',
                episode_imdb=episode_imdb if episode and isinstance(episode_imdb, str) and re.fullmatch(r'tt[0-9]{5,10}', episode_imdb) else '',
                tmdb=query.get('parent_tmdb_id') or query.get('tmdb_id'))


def search(handle, base, params, providers):
    media = make_media(xbmc.Player(), params)
    results, failures = providers.search(media)
    if failures:
        xbmc.log('MDM Subs provider unavailable: '+', '.join(sorted(failures)), xbmc.LOGWARNING)
    for row in results:
        label = xbmc.convertLanguage(row.language, xbmc.ENGLISH_NAME) or row.language
        entry = xbmcgui.ListItem(label=label, label2=f'{row.name} Ã‚Â· {NAMES[row.provider]}')
        entry.setProperty('hearing_imp', 'true' if row.hi else 'false')
        entry.setProperty('sync', 'true' if row.sync else 'false')
        entry.setArt({'thumb': row.language})
        candidates = [dict(provider=row.provider, id=row.id)]
        for fallback in results:
            if (fallback.provider != row.provider and fallback.language == row.language
                    and fallback.hi == row.hi and normalise(row.name) == normalise(fallback.name)):
                candidates.append(dict(provider=fallback.provider, id=fallback.id))
                if len(candidates) == 3:
                    break
        url = base + '?' + urlencode({'action': 'download', 'candidates': json.dumps(candidates),
                                      'episode': json.dumps(media['episode']), 'release': media['release']})
        xbmcplugin.addDirectoryItem(handle, url, entry, isFolder=False)


def download(handle, params, providers):
    if params.get('file_id', '').isdigit():
        candidates = [dict(provider='open', id=params['file_id'])]
    else:
        candidates = json.loads(params.get('candidates', '[]'))
    episode = json.loads(params.get('episode', 'null'))
    if not isinstance(candidates, list) or not candidates or len(candidates) > 3:
        raise SubtitleError('Select a subtitle from the search results.')
    for row in candidates:
        try:
            if not isinstance(row, dict) or row.get('provider') not in NAMES or not isinstance(row.get('id'), str):
                continue
            content = providers.download(row, tuple(episode) if episode else None, params.get('release', ''))
            break
        except (SubtitleError, OSError, ValueError, TypeError, EOFError):
            continue
    else:
        raise SubtitleError('Could not download this subtitle. Try another result.')
    folder = Path(xbmcvfs.translatePath('special://temp/'))
    path = folder / f'mdm-subs-{uuid4().hex}.srt'
    with path.open('w', encoding='utf-8', newline='') as subtitle:
        subtitle.write(content)
    xbmcplugin.addDirectoryItem(handle, str(path), xbmcgui.ListItem(path=str(path)), isFolder=False)


def run(argv):
    base, handle, query = argv[:3]
    handle = int(handle)
    params = {key: values[0] for key, values in parse_qs(query.lstrip('?')).items()}
    try:
        action = params.get('action')
        if action in ('search', 'manualsearch', 'download'):
            client = Providers(xbmcaddon.Addon().getSetting, language_code,
                               lambda code: xbmc.convertLanguage(code, xbmc.ENGLISH_NAME) or code,
                               lambda code: xbmc.convertLanguage(code, xbmc.ISO_639_2) or code)
            if action == 'download':
                download(handle, params, client)
            else:
                search(handle, base, params, client)
    except SubtitleError as error:
        xbmcgui.Dialog().notification('MDM Subs', str(error))
    except (OSError, RuntimeError, ValueError):
        xbmcgui.Dialog().notification('MDM Subs', 'Kodi could not read the video details or save the subtitle.')
    finally:
        xbmcplugin.endOfDirectory(handle, cacheToDisc=False)


if __name__ == '__main__':
    run(sys.argv)
