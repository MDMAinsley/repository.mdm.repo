"""Kodi subtitle search and download entry point."""
import json
import ntpath
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from uuid import uuid4

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from opensubtitles import OpenSubtitles, SubtitleError, normalise


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
            raise SubtitleError('Episode details are missing. Try Kodi’s manual subtitle search.')
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
        raise SubtitleError('Film details are missing. Try Kodi’s manual subtitle search.')
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


def search(handle, base, params, client):
    player = xbmc.Player()
    item = player.getPlayingItem()
    query = search_identity(params, item.getVideoInfoTag(), flix_identity(item))
    preferred = language_code(params.get('preferredlanguage', ''))
    languages = [code for label in params.get('languages', '').split(',')
                 if (code := language_code(label))]
    if languages or preferred:
        query['languages'] = ','.join(sorted(set(languages or [preferred])))
    path = player.getPlayingFile().split('|', 1)[0]
    release = normalise(ntpath.basename(unquote(urlsplit(path).path)))
    for _, file_id, language, name, hearing_impaired in client.search(query, preferred, release):
        label = xbmc.convertLanguage(language, xbmc.ENGLISH_NAME) or language
        entry = xbmcgui.ListItem(label=label, label2=name)
        entry.setProperty('hearing_imp', 'true' if hearing_impaired else 'false')
        entry.setProperty('sync', 'false')
        entry.setArt({'thumb': language})
        url = base + '?' + urlencode({'action': 'download', 'file_id': file_id})
        xbmcplugin.addDirectoryItem(handle, url, entry, isFolder=False)


def download(handle, params, client):
    file_id = number(params.get('file_id'))
    if file_id is None:
        raise SubtitleError('Select a subtitle from the search results.')
    content = client.download(file_id)
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
            client = OpenSubtitles(xbmcaddon.Addon().getSetting('api_key'))
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
