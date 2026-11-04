"""Mirror GitHub branches to Gitea while preserving displaced branch heads."""
import os
import subprocess
import sys


SOURCE = 'refs/remotes/origin/'
TARGET = 'refs/idv-gitea-mirror/'


def git(*args, capture=False, allowed=(0,)):
    result = subprocess.run(['git', *args], text=True,
                            stdout=subprocess.PIPE if capture else None)
    if result.returncode not in allowed:
        raise RuntimeError(f'git {args[0]} failed (exit {result.returncode})')
    return result


def refs(prefix):
    output = git('for-each-ref', '--format=%(refname) %(objectname)', prefix,
                 capture=True).stdout
    return {name[len(prefix):]: oid for name, oid in
            (line.split(' ', 1) for line in output.splitlines())}


def main():
    remote = os.environ['GITEA_SSH_URL']
    git('fetch', '--quiet', '--no-tags', '--prune', remote,
        f'+refs/heads/*:{TARGET}*')
    source = refs(SOURCE)
    source.pop('HEAD', None)
    target = refs(TARGET)
    if not source or 'main' not in source:
        raise RuntimeError('GitHub branch snapshot is incomplete')

    leases, updates, backups = [], [], []
    for name, oid in sorted(source.items()):
        destination = 'refs/heads/' + name
        previous = target.get(name, '')
        leases.append(f'--force-with-lease={destination}:{previous}')
        updates.append(f'{oid}:{destination}')
        if previous and previous != oid:
            ancestry = git('merge-base', '--is-ancestor', previous, oid,
                           allowed=(0, 1)).returncode
            if ancestry == 1:
                backup = f'refs/tags/mirror-backup/{name}/{previous}'
                backups.append(f'{previous}:{backup}')
                print(f'Preserving previous Gitea branch head: {name}', flush=True)

    # Each lease protects the value observed above; atomic push ensures a failed
    # backup, tag, or branch update cannot leave a partial mirror.
    git('push', '--atomic', '--tags', *leases, remote, *updates, *backups)
    print(f'Mirrored {len(source)} branches; preserved {len(backups)} displaced heads.')


if __name__ == '__main__':
    try:
        main()
    except (KeyError, RuntimeError, OSError) as error:
        print(f'Gitea mirror failed: {error}', file=sys.stderr)
        sys.exit(1)
