import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

FIXTURES = ROOT / "tests" / "fixtures"

CLIENT_LIST = "admin/client?form=client_list"
PERFORMANCE = "admin/network?form=performance"
DEVICE_LIST = "admin/device?form=device_list"


def load(name):
    """The `result` block, which is what TPLinkDecoClient.request() returns."""
    return json.loads((FIXTURES / name).read_text())["result"]


class FakeDecoClient:
    """Stands in for tplinkrouterc6u.TPLinkDecoClient.

    Same surface the exporter uses: authorize(), request(path, data), logout().
    `responses` maps a path to a result dict or to an Exception to raise.
    `log` is shared across every client a factory hands out, so a test can see
    the order of calls across re-logins.
    """

    def __init__(self, log, responses, fail_login=None):
        self.log = log
        self.responses = responses
        self.fail_login = fail_login
        self.logged_in = False

    def authorize(self):
        self.log.append(("authorize",))
        if self.fail_login:
            raise self.fail_login
        self.logged_in = True

    def request(self, path, data, ignore_response=False, ignore_errors=False):
        self.log.append(("request", path, json.loads(data)))
        if not self.logged_in:
            raise Exception("Not authorised")
        r = self.responses.get(path)
        if isinstance(r, list):  # a sequence: one entry per call
            r = r.pop(0)
        if isinstance(r, BaseException):
            raise r
        if r is None:
            raise AssertionError(f"unexpected path {path}")
        return r

    def logout(self):
        self.log.append(("logout",))
        self.logged_in = False


class FakeFactory:
    def __init__(self, responses=None, fail_login=None):
        self.log = []
        self.calls = []
        self.responses = responses if responses is not None else default_responses()
        self.fail_login = fail_login

    def __call__(self, host, password, username, timeout):
        self.calls.append(dict(host=host, password=password, username=username, timeout=timeout))
        fail, self.fail_login = self._next_login_failure()
        return FakeDecoClient(self.log, self.responses, fail)

    def _next_login_failure(self):
        f = self.fail_login
        if isinstance(f, list):
            return (f.pop(0) if f else None), f
        return f, f

    def paths(self):
        return [e[1] for e in self.log if e[0] == "request"]


def default_responses():
    return {
        CLIENT_LIST: load("client_list.json"),
        PERFORMANCE: load("performance.json"),
        DEVICE_LIST: load("device_list.json"),
    }


@pytest.fixture
def factory():
    return FakeFactory()
