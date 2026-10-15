"""Resolve public official OCI manifests over HTTPS without downloading or running images."""
import hashlib
import json
import re
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

ACCEPT = ', '.join(('application/vnd.oci.image.index.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
    'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'))


def manifest(host, repository, tag):
    url = f'https://{host}/v2/{repository}/manifests/{tag}'
    headers = {'Accept': ACCEPT, 'Accept-Encoding': 'identity'}
    try:
        response = urlopen(Request(url, headers=headers), timeout=40)
    except HTTPError as error:
        if error.code != 401:
            raise
        challenge = error.headers.get('WWW-Authenticate', '')
        if not challenge.lower().startswith('bearer '):
            raise ValueError('Unknown official registry authentication challenge')
        values = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = values.pop('realm', '')
        parsed = urlparse(realm)
        if parsed.scheme != 'https' or parsed.hostname not in ('auth.docker.io', 'quay.io'):
            raise ValueError('Unexpected token issuer; refusing credential request')
        with urlopen(realm + '?' + urlencode(values), timeout=40) as token_response:
            token = json.load(token_response)
        headers['Authorization'] = 'Bearer ' + (token.get('token') or token.get('access_token'))
        response = urlopen(Request(url, headers=headers), timeout=40)
    with response:
        payload = response.read(16 * 1024 * 1024)
        digest = 'sha256:' + hashlib.sha256(payload).hexdigest()
        supplied = response.headers.get('Docker-Content-Digest')
        if supplied and supplied != digest:
            raise ValueError('Manifest digest mismatch')
        document = json.loads(payload)
        return {'tag': tag, 'manifestUrl': url, 'digest': digest, 'bytes': len(payload),
                'platforms': sorted({f"{m['platform'].get('os')}/{m['platform'].get('architecture')}" for m in document.get('manifests', []) if m.get('platform')})}


def main():
    result = {
        'python': manifest('registry-1.docker.io', 'library/python', '3.12.14-slim-bookworm'),
        'postgresql': manifest('registry-1.docker.io', 'library/postgres', '16.15-bookworm'),
        'keycloak': manifest('quay.io', 'keycloak/keycloak', '26.8.0'),
    }
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
