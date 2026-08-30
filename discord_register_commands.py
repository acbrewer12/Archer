#!/usr/bin/env python3
"""
One-time (or on-change) registration of Archer's Discord slash commands.

Run manually after creating the bot and setting DISCORD_BOT_TOKEN /
DISCORD_APPLICATION_ID. Global command registration can take up to an hour
to propagate on Discord's side, so this is intentionally NOT run on every
server boot — only run it again when the command list below changes.

Usage:
    DISCORD_BOT_TOKEN=... DISCORD_APPLICATION_ID=... python3 discord_register_commands.py
"""
import json
import os
import urllib.request

BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '')
APP_ID    = os.environ.get('DISCORD_APPLICATION_ID', '')

COMMANDS = [
    {
        'name': 'status',
        'description': 'Live truck vitals — RPM, oil, coolant, boost, battery, weather.',
    },
    {
        'name': 'ask',
        'description': 'Ask Archer anything.',
        'options': [
            {'type': 3, 'name': 'question', 'description': 'What to ask', 'required': True},
        ],
    },
    {
        'name': 'parking',
        'description': 'Arm or disarm parking mode / surveillance.',
        'options': [{
            'type': 3, 'name': 'action', 'description': 'arm or disarm', 'required': True,
            'choices': [
                {'name': 'Arm',    'value': 'arm'},
                {'name': 'Disarm', 'value': 'disarm'},
            ],
        }],
    },
    {
        'name': 'digest',
        'description': 'Send the current session digest now.',
    },
]


def main():
    if not BOT_TOKEN or not APP_ID:
        raise SystemExit('Set DISCORD_BOT_TOKEN and DISCORD_APPLICATION_ID first.')

    data = json.dumps(COMMANDS).encode()
    req = urllib.request.Request(
        f'https://discord.com/api/v10/applications/{APP_ID}/commands',
        data=data,
        headers={'Authorization': f'Bot {BOT_TOKEN}', 'Content-Type': 'application/json'},
        method='PUT',
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        print(f'Registered {len(COMMANDS)} commands — status {r.status}')
        print(r.read().decode())


if __name__ == '__main__':
    main()
