"""CLI access to the existing authenticated loopback dashboard queue."""
import json
from urllib.parse import urlsplit
import urllib.error
import urllib.request


def request(endpoint, body=None):
    from dashboard import session_url
    address = session_url()
    if not address:
        raise ValueError('Start codehub web or codehub app first to use the shared task queue.')
    parsed=urlsplit(address)
    headers={'X-CodeHub-Token':parsed.fragment}
    data=None
    if body is not None:
        headers['Content-Type']='application/json';data=json.dumps(body).encode()
    call=urllib.request.Request(f'{parsed.scheme}://{parsed.netloc}/api/{endpoint}',data=data,headers=headers)
    try:
        with urllib.request.urlopen(call,timeout=10) as response: return json.load(response)
    except urllib.error.HTTPError as error:
        raise ValueError(json.load(error).get('error','Queue request failed.')) from None


def control(action='list', identifier=None):
    if action == 'list': return request('queue')
    if action in ('pause','resume'): return request('queue',{'action':action})
    if not identifier: raise ValueError('Supply a queued task ID.')
    if action == 'cancel': return request('stop',{'id':identifier})
    if action in ('up','down'): return request('queue',{'action':'move','id':identifier,'direction':action})
    raise ValueError('Use queue list, pause, resume, up, down, or cancel.')
