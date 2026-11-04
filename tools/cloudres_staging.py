"""Resolve a safe GitHub staging snapshot for cloud configuration updates."""
import re

STAGING_BRANCH = 'cloud-config-staging'


def staging_snapshot(session, root):
    """Merge a fixed main snapshot into staging; stop on any merge conflict."""
    def get(path):
        response = session.get(root + path, timeout=30)
        response.raise_for_status()
        return response.json()

    main_sha = get('/commits/main')['sha']
    staging_sha = get('/commits/' + STAGING_BRANCH)['sha']
    if not all(re.fullmatch(r'[0-9a-fA-F]{40}', value or '')
               for value in (main_sha, staging_sha)):
        raise ValueError('GitHub returned an invalid commit SHA')
    comparison = get(f'/compare/{main_sha}...{staging_sha}')
    if comparison.get('merge_base_commit', {}).get('sha') != main_sha:
        response = session.post(root + '/merges', timeout=30, json={
            'base': STAGING_BRANCH, 'head': main_sha,
            'commit_message': 'Merge main into cloud-config-staging',
        })
        if response.status_code == 409:
            raise RuntimeError('main cannot be merged into cloud-config-staging automatically; resolve conflicts without overwriting pending changes')
        response.raise_for_status()
        staging_sha = get('/commits/' + STAGING_BRANCH)['sha']
        if not re.fullmatch(r'[0-9a-fA-F]{40}', staging_sha or ''):
            raise ValueError('GitHub returned an invalid cloud-config-staging commit SHA')
        comparison = get(f'/compare/{main_sha}...{staging_sha}')
        if comparison.get('merge_base_commit', {}).get('sha') != main_sha:
            raise RuntimeError('cloud-config-staging changed during merge; retry without overwriting pending changes')
    return main_sha, staging_sha
