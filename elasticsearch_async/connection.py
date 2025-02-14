import asyncio
import ssl
import warnings

import aiohttp
from aiohttp.client_exceptions import ServerFingerprintMismatch
import async_timeout

from elasticsearch.exceptions import ConnectionError, ConnectionTimeout, ImproperlyConfigured, SSLError
from elasticsearch.connection import Connection
from elasticsearch.compat import urlencode
from elasticsearch.connection.http_urllib3 import create_ssl_context


class AIOHttpConnection(Connection):
    def __init__(self, host='localhost', port=9200, http_auth=None,
            use_ssl=False, verify_certs=False, ca_certs=None, client_cert=None,
            client_key=None, loop=None, use_dns_cache=True, headers=None,
            ssl_context=None, **kwargs):
        super().__init__(host=host, port=port, **kwargs)

        self.loop = asyncio.get_event_loop() if loop is None else loop

        if http_auth is not None:
            if isinstance(http_auth, str):
                http_auth = tuple(http_auth.split(':', 1))

            if isinstance(http_auth, (tuple, list)):
                http_auth = aiohttp.BasicAuth(*http_auth)

        headers = headers or {}
        headers.setdefault('content-type', 'application/json')

        # if providing an SSL context, raise error if any other SSL related flag is used
        if ssl_context and (verify_certs or ca_certs):
            raise ImproperlyConfigured("When using `ssl_context`, `use_ssl`, `verify_certs`, `ca_certs` are not permitted")

        if use_ssl or ssl_context:
            cafile = ca_certs
            if not cafile and not ssl_context and verify_certs:
                # If no ca_certs and no sslcontext passed and asking to verify certs
                # raise error
                raise ImproperlyConfigured("Root certificates are missing for certificate "
                    "validation. Either pass them in using the ca_certs parameter or "
                    "install certifi to use it automatically.")
            if verify_certs or ca_certs:
                warnings.warn('Use of `verify_certs`, `ca_certs` have been deprecated in favor of using SSLContext`', DeprecationWarning)

            if not ssl_context:
                # if SSLContext hasn't been passed in, create one.
                # need to skip if sslContext isn't avail
                try:
                    ssl_context = create_ssl_context(cafile=cafile)
                except AttributeError:
                    ssl_context = None

                if not verify_certs and ssl_context is not None:
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE
                    warnings.warn(
                        'Connecting to %s using SSL with verify_certs=False is insecure.' % host)
            if ssl_context:
                verify_certs = True
                use_ssl = True

        self.session = aiohttp.ClientSession(
            auth=http_auth,
            conn_timeout=self.timeout,
            connector=aiohttp.TCPConnector(
                loop=self.loop,
                verify_ssl=verify_certs,
                use_dns_cache=use_dns_cache,
                ssl_context=ssl_context,
            ),
            headers=headers
        )

        self.base_url = 'http%s://%s:%d%s' % (
            's' if use_ssl else '',
            host, port, self.url_prefix
        )

    async def close(self):
        await self.session.close()

    async def perform_request(self, method, url, params=None, body=None, timeout=None, ignore=(), headers=None):
        url_path = url
        if params:
            url_path = '%s?%s' % (url, urlencode(params or {}))
        url = self.base_url + url_path

        start = self.loop.time()
        response = None
        try:
            with async_timeout.timeout(timeout or self.timeout, loop=self.loop):
                response = await self.session.request(method, url, data=body, headers=headers)
                raw_data = await response.text()
            duration = self.loop.time() - start

        except asyncio.CancelledError:
            raise

        except Exception as e:
            self.log_request_fail(method, url, url_path, body, self.loop.time() - start, exception=e)
            if isinstance(e, ServerFingerprintMismatch):
                raise SSLError('N/A', str(e), e)
            if isinstance(e, asyncio.TimeoutError):
                raise ConnectionTimeout('TIMEOUT', str(e), e)
            raise ConnectionError('N/A', str(e), e)

        finally:
            if response is not None:
                await response.release()

        # raise errors based on http status codes, let the client handle those if needed
        if not (200 <= response.status < 300) and response.status not in ignore:
            self.log_request_fail(method, url, url_path, body, duration, status_code=response.status, response=raw_data)
            self._raise_error(response.status, raw_data)

        self.log_request_success(method, url, url_path, body, response.status, raw_data, duration)

        return response.status, response.headers, raw_data
