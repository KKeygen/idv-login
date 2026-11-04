"""Update cloud resources without resolving a beta through releases/latest."""
import base64
import json
import os
import re
import time
from urllib.parse import quote

import markdown
import requests

from cloudres_staging import STAGING_BRANCH, staging_snapshot


def merge_resources(data, release, html, announcement, gitee_repo, critical=False):
    result = dict(data)
    if release is not None:
        if release.get('draft') or not re.fullmatch(r'v\d+(?:\.\d+)+-(?:stable|beta)', release['tag_name']):
            raise ValueError('A published, exact version tag is required')
        result['version'] = release['tag_name']
        result['downloadUrl'] = f"https://gitee.com/{gitee_repo}/releases/tag/{release['tag_name']}"
        result['detail'] = release.get('body') or ''
        result['detail_html'] = markdown.markdown(result['detail'], extensions=['tables'])
    result['lastModified'] = int(time.time())
    result['login_base64_page'] = base64.b64encode(html.encode('utf-8')).decode('ascii')
    result['announcement'] = announcement
    result['critical_update'] = critical or data.get('critical_update', False)
    return result


def main():
    repo = os.environ['GITHUB_REPOSITORY']
    tag = os.getenv('RELEASE_TAG', '').strip()
    if tag and not re.fullmatch(r'v\d+(?:\.\d+)+-(?:stable|beta)', tag):
        raise ValueError('Invalid release tag')
    session = requests.Session()
    session.headers.update({'Authorization': f"Bearer {os.environ['GITHUB_TOKEN']}",
                            'Accept': 'application/vnd.github+json'})
    root = f'https://api.github.com/repos/{repo}'

    def get(path, **kwargs):
        response = session.get(root + path, timeout=30, **kwargs)
        response.raise_for_status()
        return response.json()

    release = get('/releases/tags/' + quote(tag, safe='')) if tag else None
    if release is not None and release.get('tag_name') != tag:
        raise ValueError('Release tag mismatch')
    for attempt in range(3):
        # Keep pending staged edits and read page/announcement from one main snapshot.
        # Merge the pinned main snapshot first; conflicts stop candidate generation.
        main_commit, staged_commit = staging_snapshot(session, root)

        def content(path, commit):
            item = get('/contents/' + path, params={'ref': commit})
            return item, base64.b64decode(item['content']).decode('utf-8')

        info, raw = content('assets/cloudRes.json', staged_commit)
        _, html = content('assets/index.html', main_commit)
        _, announcement = content('assets/anno', main_commit)
        data = merge_resources(json.loads(raw), release, html, announcement,
                               os.environ['GITEE_ROPE'],
                               os.getenv('CRITICAL_UPDATE', 'false').lower() == 'true')
        encoded = base64.b64encode((json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()).decode()
        response = session.put(root + '/contents/assets/cloudRes.json', timeout=30, json={
            'message': 'Stage cloudRes.json - JSON resources updated',
            'content': encoded, 'sha': info['sha'], 'branch': STAGING_BRANCH,
        })
        if response.status_code in (200, 201):
            print(f"cloudRes.json staged (not published): {data.get('version', '')}")
            return
        if response.status_code not in (409, 422):
            response.raise_for_status()
        if attempt < 2:
            time.sleep(5)
    raise RuntimeError('cloud-config-staging cloudRes update rejected or changed concurrently; no update was committed')


if __name__ == '__main__':
    main()
