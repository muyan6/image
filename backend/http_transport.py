"""Bounded per-thread metadata connections, with no request replay or credentials.

Only the gateway JSON and COS control clients use this transport. Response
ownership remains with the caller, including bounded streaming and Range GETs.
"""
import atexit
import threading
import weakref
from http.cookiejar import DefaultCookiePolicy

import requests
from requests.adapters import HTTPAdapter


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


_local = threading.local()
_owners = weakref.WeakSet()
_registry_lock = threading.Lock()


class _ThreadSession:
    def __init__(self):
        self.closed = False
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.cookies.set_policy(_NoCookies())
        # Each worker sends one synchronous request at a time. Limit cached
        # origins as well as idle connections; never add automatic POST retries.
        for scheme in ('http://', 'https://'):
            self.session.mount(scheme, HTTPAdapter(pool_connections=8, pool_maxsize=1, max_retries=0))
        with _registry_lock:_owners.add(self)

    def close(self):
        with self._lock:
            if self.closed:return
            self.closed = True
        self.session.close()

    def __del__(self):
        # Thread-local storage releases its owner when a worker exits. A weak
        # registry does not retain old threads or their sockets until shutdown.
        try:self.close()
        except Exception:pass


def get_session():
    """Return this worker's Session, recreating it after an explicit close."""
    owner = getattr(_local, 'owner', None)
    if owner is None or owner.closed:
        owner = _ThreadSession()
        _local.owner = owner
    return owner.session


def request(method, url, **kwargs):
    """Reuse only connections; headers/auth/cookies remain request-local."""
    session = get_session()
    # Clients supply signed headers for each call. Never persist a supplier's
    # cookies or authentication on subsequent COS or other account requests.
    session.cookies.clear()
    kwargs['allow_redirects'] = False
    return session.request(method, url, **kwargs)


def close_current_thread():
    """Close and forget this worker's Session; repeated closes are harmless."""
    owner = getattr(_local, 'owner', None)
    if owner is not None:
        del _local.owner
        owner.close()


def close_all():
    """Close live Sessions after workers drain; future requests recreate them.

    This shutdown/restart hook must run after active metadata requests finish.
    The registry is weak and contains at most one current owner per live thread.
    """
    with _registry_lock:owners = list(_owners)
    for owner in owners:owner.close()


atexit.register(close_all)
