# -*- coding: utf-8 -*-

# Copyright (C) 2014-2016  RhodeCode GmbH
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License, version 3
# (only), as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# This program is dual-licensed. If you wish to learn more about the
# RhodeCode Enterprise Edition, including its added features, Support services,
# and proprietary license terms, please see https://rhodecode.com/licenses/

"""
Provides the implementation of various client utilities to reach the vcsserver.
"""


import copy
import logging
import threading
import urlparse
import uuid
import weakref
from urllib2 import URLError

import msgpack
import Pyro4
import requests
from Pyro4.errors import CommunicationError, ConnectionClosedError, DaemonError

from rhodecode.lib.vcs import exceptions
from rhodecode.lib.vcs.conf import settings

log = logging.getLogger(__name__)


# TODO: mikhail: Keep it in sync with vcsserver's
# HTTPApplication.ALLOWED_EXCEPTIONS
EXCEPTIONS_MAP = {
    'KeyError': KeyError,
    'URLError': URLError,
}


class HTTPRepoMaker(object):
    def __init__(self, server_and_port, backend_endpoint):
        self.url = urlparse.urljoin(
            'http://%s' % server_and_port, backend_endpoint)

    def __call__(self, path, config, with_wire=None):
        log.debug('HTTPRepoMaker call on %s', path)
        return HTTPRemoteRepo(path, config, self.url, with_wire=with_wire)

    def __getattr__(self, name):
        def f(*args, **kwargs):
            return self._call(name, *args, **kwargs)
        return f

    @exceptions.map_vcs_exceptions
    def _call(self, name, *args, **kwargs):
        payload = {
            'id': str(uuid.uuid4()),
            'method': name,
            'params': {'args': args, 'kwargs': kwargs}
        }
        return _remote_call(self.url, payload, EXCEPTIONS_MAP)


class VcsHttpProxy(object):

    CHUNK_SIZE = 16384

    def __init__(self, server_and_port, backend_endpoint):
        adapter = requests.adapters.HTTPAdapter(max_retries=5)
        self.base_url = urlparse.urljoin(
            'http://%s' % server_and_port, backend_endpoint)
        self.session = requests.Session()
        self.session.mount('http://', adapter)

    def handle(self, environment, input_data, *args, **kwargs):
        data = {
            'environment': environment,
            'input_data': input_data,
            'args': args,
            'kwargs': kwargs
        }
        result = self.session.post(
            self.base_url, msgpack.packb(data), stream=True)
        return self._get_result(result)

    def _deserialize_and_raise(self, error):
        exception = Exception(error['message'])
        try:
            exception._vcs_kind = error['_vcs_kind']
        except KeyError:
            pass
        raise exception

    def _iterate(self, result):
        unpacker = msgpack.Unpacker()
        for line in result.iter_content(chunk_size=self.CHUNK_SIZE):
            unpacker.feed(line)
            for chunk in unpacker:
                yield chunk

    def _get_result(self, result):
        iterator = self._iterate(result)
        error = iterator.next()
        if error:
            self._deserialize_and_raise(error)

        status = iterator.next()
        headers = iterator.next()

        return iterator, status, headers


class HTTPRemoteRepo(object):
    def __init__(self, path, config, url, with_wire=None):
        self.url = url
        self._wire = {
            "path": path,
            "config": config,
            "context": str(uuid.uuid4()),
        }
        if with_wire:
            self._wire.update(with_wire)

    def __getattr__(self, name):
        def f(*args, **kwargs):
            return self._call(name, *args, **kwargs)
        return f

    @exceptions.map_vcs_exceptions
    def _call(self, name, *args, **kwargs):
        log.debug('Calling %s@%s', self.url, name)
        # TODO: oliver: This is currently necessary pre-call since the
        # config object is being changed for hooking scenarios
        wire = copy.deepcopy(self._wire)
        wire["config"] = wire["config"].serialize()
        payload = {
            'id': str(uuid.uuid4()),
            'method': name,
            'params': {'wire': wire, 'args': args, 'kwargs': kwargs}
        }
        return _remote_call(self.url, payload, EXCEPTIONS_MAP)

    def __getitem__(self, key):
        return self.revision(key)


def _remote_call(url, payload, exceptions_map):
    response = requests.post(url, data=msgpack.packb(payload))
    response = msgpack.unpackb(response.content)
    error = response.get('error')
    if error:
        type_ = error.get('type', 'Exception')
        exc = exceptions_map.get(type_, Exception)
        exc = exc(error.get('message'))
        try:
            exc._vcs_kind = error['_vcs_kind']
        except KeyError:
            pass
        raise exc
    return response.get('result')


class RepoMaker(object):

    def __init__(self, proxy_factory):
        self._proxy_factory = proxy_factory

    def __call__(self, path, config, with_wire=None):
        log.debug('RepoMaker call on %s', path)
        return RemoteRepo(
            path, config, remote_proxy=self._proxy_factory(),
            with_wire=with_wire)

    def __getattr__(self, name):
        remote_proxy = self._proxy_factory()
        func = _get_proxy_method(remote_proxy, name)
        return _wrap_remote_call(remote_proxy, func)


class ThreadlocalProxyFactory(object):
    """
    Creates one Pyro4 proxy per thread on demand.
    """

    def __init__(self, remote_uri):
        self._remote_uri = remote_uri
        self._thread_local = threading.local()

    def __call__(self):
        if not hasattr(self._thread_local, 'proxy'):
            self._thread_local.proxy = Pyro4.Proxy(self._remote_uri)
        return self._thread_local.proxy


class RemoteRepo(object):

    def __init__(self, path, config, remote_proxy, with_wire=None):
        self._wire = {
            "path": path,
            "config": config,
            "context": uuid.uuid4(),
        }
        if with_wire:
            self._wire.update(with_wire)
        self._remote_proxy = remote_proxy
        self.refs = RefsWrapper(self)

    def __getattr__(self, name):
        log.debug('Calling %s@%s', self._remote_proxy, name)
        # TODO: oliver: This is currently necessary pre-call since the
        # config object is being changed for hooking scenarios
        wire = copy.deepcopy(self._wire)
        wire["config"] = wire["config"].serialize()

        try:
            func = _get_proxy_method(self._remote_proxy, name)
        except DaemonError as e:
            if e.message == 'unknown object':
                raise exceptions.VCSBackendNotSupportedError
            else:
                raise

        return _wrap_remote_call(self._remote_proxy, func, wire)

    def __getitem__(self, key):
        return self.revision(key)


def _get_proxy_method(proxy, name):
    try:
        return getattr(proxy, name)
    except CommunicationError:
        raise CommunicationError(
            'Unable to connect to remote pyro server %s' % proxy)


def _wrap_remote_call(proxy, func, *args):
    all_args = list(args)

    @exceptions.map_vcs_exceptions
    def caller(*args, **kwargs):
        all_args.extend(args)
        try:
            return func(*all_args, **kwargs)
        except ConnectionClosedError:
            log.debug('Connection to VCSServer closed, trying to reconnect.')
            proxy._pyroReconnect(tries=settings.PYRO_RECONNECT_TRIES)

            return func(*all_args, **kwargs)

    return caller


class RefsWrapper(object):

    def __init__(self, repo):
        self._repo = weakref.proxy(repo)

    def __setitem__(self, key, value):
        self._repo._assign_ref(key, value)


class FunctionWrapper(object):

    def __init__(self, func, wire):
        self._func = func
        self._wire = wire

    @exceptions.map_vcs_exceptions
    def __call__(self, *args, **kwargs):
        return self._func(self._wire, *args, **kwargs)