"""Stage immutable releases with checksum-selected hardlinks and a disk reserve."""
import argparse
import os
import re
import stat
from pathlib import Path
import shutil
import subprocess

def stage(source: Path, target: Path, previous: Path | None, reserve_bytes: int, *, consume_upload: bool = False):
    if not source.is_dir() or source.is_symlink() or reserve_bytes < 0:
        raise ValueError('invalid staging source/reserve')
    if not target.is_dir() or target.is_symlink() or any(target.iterdir()):
        raise ValueError('target must be an empty real directory')
    if previous is not None:
        previous = previous.resolve(strict=True)
        if previous.parent != target.parent.resolve() or previous == target.resolve() or not previous.name.startswith('web-data.release-'):
            raise ValueError('previous release must be an immutable sibling generation')
    if consume_upload:
        stamp = r'[0-9]{8}T[0-9]{6}Z-[0-9]+'
        if (source.parent.resolve() != target.parent.resolve()
                or not re.fullmatch(r'web-data\.incoming-' + stamp, source.name)
                or not re.fullmatch(r'web-data\.release-' + stamp, target.name)):
            raise ValueError('only a completed upload next to its new release can be promoted')
    total = 0
    for path in source.rglob('*'):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('release source contains a symlink or special file')
        if path.is_file():
            total += path.stat().st_size
    required = reserve_bytes if consume_upload else total + reserve_bytes
    if shutil.disk_usage(target).free < required:
        raise OSError('insufficient free space for publication and reserve')
    if consume_upload:
        # rsync has closed the upload. Rename on the same filesystem keeps its
        # inodes and the full reserve. The publisher validates this directory
        # before switching the serving symlink, and retains the previous release.
        # Match the empty release directory's traversal permissions; an
        # uploader can keep its unfinished incoming directory private.
        source.chmod(stat.S_IMODE(target.stat().st_mode))
        os.replace(source, target)
        return
    command = ['rsync', '-a', '--checksum', '--no-times', '--no-owner', '--no-group', '--no-perms', '--delete']
    if previous is not None:
        command.append('--link-dest=' + str(previous))
    # Never --inplace: the previous published inode is read-only history.
    subprocess.run(command + [str(source.resolve())+'/',str(target.resolve())+'/'],check=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path);parser.add_argument('target',type=Path)
    parser.add_argument('--previous',type=Path);parser.add_argument('--reserve-mib',type=int,default=2048)
    parser.add_argument('--consume-upload', action='store_true', help='rename a completed, disposable sibling upload into its release directory')
    args=parser.parse_args();stage(args.source,args.target,args.previous,args.reserve_mib*1024*1024,consume_upload=args.consume_upload)

if __name__=='__main__':main()
