#! /usr/bin/env python3
#
# Copyright 2024 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json

try:
    import rados
except ImportError:
    rados = None

VERSION = 1


class RadosMap:
    def __init__(self, pool_name, logger):
        self.pool_name = pool_name
        self.logger = logger
        self.cluster = None
        self.ioctx = None

    def add_cluster(self, app_name, key, mon_host):
        if self.ioctx is not None:
            self.logger.warning('already connected to cluster')
            return

        rd = rados.Rados(name='client.' + app_name,
                         conf=dict(key=key, mon_host=mon_host))
        rd.connect()
        try:
            self.ioctx = rd.open_ioctx(self.pool_name)
        except Exception:
            rd.shutdown()
            raise

        self.cluster = rd
        self.logger.info('connected to cluster')

    def get_global_map(self):
        size = 8192   # default size
        num = 0
        while True:
            num += 1
            if num > 100:
                raise RuntimeError('failed to get global map')
            try:
                data = self.ioctx.read('global-map', length=size)
                return json.loads(data.decode('utf8'))
            except json.decoder.JSONDecodeError:
                size += 4096
                continue
            except rados.ObjectNotFound:
                return {'version': 0, 'subsys': {}}

    def update_map(self, fn):
        if self.ioctx is None:
            raise RuntimeError('cannot update map if not connected to cluster')

        while True:
            prev = self.get_global_map()
            try:
                wx = self.ioctx.create_write_op()
                if not prev['version']:
                    wx.new(1)
                    prev['version'] = VERSION
                else:
                    wx.assert_version(self.ioctx.get_last_version())

                try:
                    fn(prev)
                except Exception as exc:
                    self.logger.exception('exception caught when updating '
                                          'global map: %s' % str(exc))
                    return

                wx.write_full(json.dumps(prev).encode('utf8'))
                self.ioctx.operate_write_op(wx, 'global-map')
                return
            except (rados.ObjectExists, rados.OSError):
                pass
            finally:
                wx.release()
