"""Render pgxio's flat Nginx directives for examples and isolated tooling.

This is not an Nginx configuration parser: read_settings only accepts the flat
pgxio snippet emitted here. Nginx remains the runtime configuration parser.
"""
import json
import shlex
from pathlib import Path

BOOLS = {'validate_rest_input', 'validate_output', 'benchmark_no_jwt',
         'db_prepared_statements', 'db_pipeline', 'development'}
NUMBERS = {'pool_acquisition_timeout_ms', 'pool_queue_limit', 'jwt_cache_max_entries',
           'jwt_cache_max_bytes', 'pool_size', 'max_websockets'}
STRINGS = {'libpq_buf', 'contract', 'issuer', 'audience', 'jwks_file'}


def quote(value):
    value = str(value)
    if any(c in value for c in '\x00\r\n'):
        raise ValueError('Nginx values cannot contain NUL or newlines')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$') + '"'


def render(settings, routes):
    lines = []
    for key, value in settings.items():
        if key == 'origins':
            lines += ['pgxio_origin ' + quote(v) + ';' for v in value]
            continue
        if value is None:
            continue
        if key in BOOLS:
            if not isinstance(value, bool):
                raise ValueError(f'{key} must be boolean')
            text = 'on' if value else 'off'
        elif key in NUMBERS:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f'{key} must be a nonnegative integer')
            text = str(value)
        elif key in STRINGS:
            if key == 'libpq_buf' and value not in ('copy', 'borrowed', 'auto'):
                raise ValueError('libpq_buf must be copy, borrowed or auto')
            text = quote(value)
        else:
            raise ValueError(f'unknown setting {key}')
        directive = 'validate_input' if key == 'validate_rest_input' else key
        lines.append(f'pgxio_{directive} {text};')
    for e in routes['endpoints']:
        lines.append('pgxio_endpoint ' + quote(e['name']) + ' ' + quote(e['function']) +
                     (' read_only' if e.get('read_only') else '') + ';')
    for s in routes.get('streams', []):
        lines.append('pgxio_stream ' + quote(s['name']) + ' ' + quote(s['authorize']) + ';')
    return '\n'.join(lines) + '\n'


def read_settings(path):
    tokens = shlex.shlex(Path(path).read_text(), posix=True, punctuation_chars=';')
    tokens.whitespace_split = True
    settings, routes = {'origins': []}, {'endpoints': [], 'streams': []}
    statement = []
    for token in tokens:
        if token != ';':
            statement.append(token)
            continue
        if not statement or not statement[0].startswith('pgxio_'):
            raise ValueError('expected flat pgxio directives')
        key, args = statement[0][6:], statement[1:]
        statement = []
        if key == 'endpoint':
            if len(args) not in (2, 3) or (len(args) == 3 and args[2] != 'read_only'):
                raise ValueError('invalid endpoint')
            routes['endpoints'].append(dict(name=args[0], function=args[1], read_only=len(args) == 3))
        elif key == 'stream':
            if len(args) != 2: raise ValueError('invalid stream')
            routes['streams'].append(dict(name=args[0], authorize=args[1]))
        else:
            if len(args) != 1: raise ValueError('expected one setting argument')
            key = 'validate_rest_input' if key == 'validate_input' else key
            value = args[0].replace('\\$', '$')
            if key == 'origin': settings['origins'].append(value)
            else:
                if key in settings: raise ValueError('duplicate setting')
                if key in BOOLS:
                    if value not in ('on', 'off'): raise ValueError('expected on/off')
                    value = value == 'on'
                elif key in NUMBERS: value = int(value)
                elif key not in STRINGS: raise ValueError('unknown setting')
                elif key == 'libpq_buf' and value not in ('copy', 'borrowed', 'auto'):
                    raise ValueError('libpq_buf must be copy, borrowed or auto')
                settings[key] = value
    if statement: raise ValueError('missing semicolon')
    return settings, routes


def schemas(catalog):
    return {'version': 2,
            'endpoints': [{k: e[k] for k in ('name', 'input', 'output')} for e in catalog['endpoints']],
            'streams': [{k: s[k] for k in ('name', 'input')} for s in catalog.get('streams', [])]}
