#!/usr/bin/env python3
"""Public, stdlib-only battle client copied into each agent's workspace.

This file contains no administrator credentials and cannot reset a trial.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen


def request(config, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(config['url'].rstrip('/') + path, data=data, headers={
        'Authorization': 'Bearer ' + config['agent_token'],
        'Content-Type': 'application/json',
    })
    with urlopen(req, timeout=120) as response:
        return json.load(response)


def safe_workspace_write(workspace, relative, data):
    """Host callers must never follow a candidate-created symlink while saving.

    Directory descriptors anchor each component; O_NOFOLLOW protects both the
    archive directory and output file. The standalone candidate client uses the
    same helper, although its OS sandbox independently limits filesystem access.
    """
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in ('.', '..') for p in parts):
        raise ValueError('Output must be a simple workspace-relative path')
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        output = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                         0o600, dir_fd=fd)
        with os.fdopen(output, 'wb') as file:
            file.write(data)
    finally:
        os.close(fd)


def safe_workspace_read(workspace, relative, max_bytes=10_000_000):
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in ('.', '..') for p in parts):
        raise ValueError('Input must be a simple workspace-relative path')
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        with os.fdopen(source, 'rb') as file:
            if os.fstat(file.fileno()).st_size > max_bytes:
                raise ValueError('Image exceeds size limit')
            raw = file.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ValueError('Image exceeds size limit')
            return raw
    finally:
        os.close(fd)


def persist_observation(observation, workspace, output='observation'):
    result = dict(observation)
    image = result.pop('screen_png_base64', None)
    if image:
        png = base64.b64decode(image, validate=True)
        digest = hashlib.sha256(png).hexdigest()
        archived = 'observation_history/' + digest + '.png'
        safe_workspace_write(workspace, archived, png)
        safe_workspace_write(workspace, output + '.png', png)
        result.update(screenshot=str(Path(workspace) / (output + '.png')),
                      archived_screenshot=archived, screenshot_sha256=digest)
    raw = (json.dumps(result, indent=2) + '\n').encode()
    state_digest = hashlib.sha256(raw).hexdigest()
    safe_workspace_write(workspace, 'observation_history/' + state_digest + '.json', raw)
    safe_workspace_write(workspace, output + '.json', raw)
    return result, image


def main():
    parser = argparse.ArgumentParser(description='Observe and control your one Cynthia battle.')
    parser.add_argument('command', choices=['observe', 'press', 'touch', 'act'])
    parser.add_argument('values', nargs='*')
    parser.add_argument('--frames', type=int, default=1)
    parser.add_argument('--out', default='observation')
    args = parser.parse_args()
    config = json.loads(Path('battle-session.json').read_text())
    if args.command == 'observe':
        result = request(config, '/v1/observe')
    else:
        if args.command == 'press':
            payload = {'buttons': [x.upper() for x in args.values], 'frames': args.frames}
        elif args.command == 'touch':
            if len(args.values) != 2:
                parser.error('touch requires x y')
            payload = {'touch': {'x': int(args.values[0]), 'y': int(args.values[1])}, 'frames': args.frames}
        else:
            if len(args.values) != 1:
                parser.error('act takes one JSON object, quoted as a shell argument')
            payload = json.loads(args.values[0])
        result = request(config, '/v1/action', payload)
    result, _ = persist_observation(result, Path.cwd(), args.out)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
