"""Offline MCP fixture for transport/integration checks; never calls an AI provider."""
import json
import os
import sys

for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request: continue
    method = request['method']
    if method == 'initialize': result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fixture', 'version': '1'}}
    elif method == 'tools/list': result = {'tools': [
        {'name': 'echo', 'description': 'Echo arguments', 'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}}}},
        {'name': 'where', 'description': 'Return working directory', 'inputSchema': {'type': 'object', 'properties': {}}}]}
    elif method == 'tools/call':
        arguments = request['params'].get('arguments', {})
        text = os.getcwd() if request['params']['name'] == 'where' else arguments.get('text', '')
        result = {'content': [{'type': 'text', 'text': text}]}
    else: result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
