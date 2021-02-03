# Copyright 2018 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from copy import deepcopy
from datetime import datetime
from os import environ
import time
from urllib.parse import urlparse

import v3io.dataplane

import mlrun
from ..platforms.iguazio import split_path
from .base import (
    DataStore,
    FileStats,
    basic_auth_header,
    get_range,
    http_get,
    http_put,
    http_head,
    http_upload,
)


V3IO_LOCAL_ROOT = "v3io"


class V3ioStore(DataStore):
    def __init__(self, parent, schema, name, endpoint=""):
        super().__init__(parent, name, schema, endpoint)
        self.endpoint = (
            self.endpoint or mlrun.mlconf.v3io_api or "v3io-webapi:8081"
        )

        token = self._secret("V3IO_ACCESS_KEY") or environ.get("V3IO_ACCESS_KEY")
        username = self._secret("V3IO_USERNAME") or environ.get("V3IO_USERNAME")
        password = self._secret("V3IO_PASSWORD") or environ.get("V3IO_PASSWORD")

        self.headers = None
        self.secure = self.kind == "v3ios"
        if self.endpoint.startswith("https://"):
            self.endpoint = self.endpoint[len("https://") :]
            self.secure = True
        elif self.endpoint.startswith("http://"):
            self.endpoint = self.endpoint[len("http://") :]
            self.secure = False

        self.auth = None
        self.token = token
        if token:
            self.headers = {"X-v3io-session-key": token}
        elif username and password:
            self.headers = basic_auth_header(username, password)

    @staticmethod
    def uri_to_ipython(endpoint, subpath):
        return V3IO_LOCAL_ROOT + subpath

    @property
    def url(self):
        schema = "https" if self.secure else "http"
        return "{}://{}".format(schema, self.endpoint)

    def upload(self, key, src_path):
        http_upload(self.url + self._join(key), src_path, self.headers, None)

    def get(self, key, size=None, offset=0):
        headers = self.headers
        if size or offset:
            headers = deepcopy(headers)
            headers["Range"] = get_range(size, offset)
        return http_get(self.url + self._join(key), headers)

    def put(self, key, data, append=False):
        http_put(self.url + self._join(key), data, self.headers, None)

    def stat(self, key):
        head = http_head(self.url + self._join(key), self.headers)
        size = int(head.get("Content-Length", "0"))
        datestr = head.get("Last-Modified", "0")
        modified = time.mktime(
            datetime.strptime(datestr, "%a, %d %b %Y %H:%M:%S %Z").timetuple()
        )
        return FileStats(size, modified)

    def listdir(self, key):
        v3io_client = v3io.dataplane.Client(
            endpoint=self.url, access_key=self.token, transport_kind="requests"
        )
        container, subpath = split_path(self._join(key))
        if not subpath.endswith("/"):
            subpath += "/"

        # without the trailing slash
        subpath_length = len(subpath) - 1

        try:
            response = v3io_client.get_container_contents(
                container=container,
                path=subpath,
                get_all_attributes=False,
                directories_only=False,
            )
        except RuntimeError as exc:
            if "Permission denied" in str(exc):
                raise mlrun.errors.MLRunAccessDeniedError(
                    f"Access denied to path: {key}"
                ) from exc
            raise

        # todo: full = key, size, last_modified
        return [obj.key[subpath_length:] for obj in response.output.contents]


def parse_v3io_path(url):
    """return v3io table path from url"""
    parsed_url = urlparse(url)
    scheme = parsed_url.scheme.lower()
    if scheme != "v3io" and scheme != "v3ios":
        raise mlrun.errors.MLRunInvalidArgumentError(
            "url must start with v3io://[host]/{container}/{path}, got " + url
        )
    endpoint = parsed_url.hostname
    if endpoint:
        if parsed_url.port:
            endpoint += ":{}".format(parsed_url.port)
        prefix = "https" if scheme == "v3ios" else "http"
        endpoint = f"{prefix}://{endpoint}"
    else:
        endpoint = None
    return endpoint, parsed_url.path.strip("/") + "/"
