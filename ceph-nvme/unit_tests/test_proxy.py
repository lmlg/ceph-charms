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
import multiprocessing
import os
import socket
import tempfile
import time
import unittest

import src.proxy as proxy
from . import utils


class MockGmap:
    BASE = {'version': 0, 'subsys': {}}

    def __init__(self, *args):
        self.gmap = self.BASE.copy()

    def add_cluster(self, *args):
        pass

    def get_global_map(self):
        return self.gmap

    def update_map(self, fn):
        tmp = self.gmap.copy()
        fn(tmp)
        self.gmap = tmp
        if not isinstance(tmp, dict):
            raise TypeError()


class TestBase(unittest.TestCase):
    GMAP_CLS = None
    LOCAL_STATE = None
    LOCAL_PORT = None
    PRE_CMDS = ()

    def setUp(self):
        self.rpc = proxy.utils.RPC()
        self.proxy_addr = ('127.0.0.1', self.LOCAL_PORT)
        self.sock_path = '/tmp/nvme-test-%d.sock' % id(self)

        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)

        rpc_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        rpc_sock.bind(self.sock_path)
        rpc_sock.listen(1)

        def _mp_spdk(sock, cmds):
            spdk = utils.MockSPDK(sock, cmds)
            while True:
                try:
                    spdk.loop()
                except Exception:
                    break

        def _mp_proxy(out):
            wdir = tempfile.TemporaryDirectory()
            wname = wdir.name
            config_path = os.path.join(wname, 'config.json')

            with open(config_path, 'w') as file:
                conf = {'proxy-port': self.LOCAL_PORT, 'pool': 'xpool',
                        'node-id': 'nx', 'discovery-port': 8009}
                file.write(json.dumps(conf))

            lstate = self.LOCAL_STATE
            if lstate is not None:
                with open(os.path.join(wname, 'local.json'), 'w') as file:
                    if not isinstance(lstate, str):
                        lstate = json.dumps(lstate)
                    file.write(lstate)

            p = proxy.Proxy(config_path, self.sock_path, self.GMAP_CLS)
            out.append(1)
            p.serve()

        mgr = multiprocessing.Manager()
        out = mgr.list()

        self.spdk = multiprocessing.Process(target=_mp_spdk,
                                            args=(rpc_sock, self.PRE_CMDS))
        self.spdk.start()
        self.proxy = multiprocessing.Process(target=_mp_proxy, args=(out,))
        self.proxy.start()
        self.local_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.local_sock.bind(('127.0.0.1', 0))

        while len(out) < 1:
            time.sleep(0)

    def tearDown(self):
        self.local_sock.sendto(b'{"method":"stop"}', self.proxy_addr)
        self.proxy.join()
        self.local_sock.close()
        self.spdk.kill()
        self.spdk.join()

    def msgloop(self, msg):
        msg = json.dumps(msg).encode('utf8')
        self.local_sock.sendto(msg, self.proxy_addr)
        return json.loads(self.local_sock.recv(2048))


class TestProxy(TestBase):
    GMAP_CLS = MockGmap
    LOCAL_PORT = 65000

    def test_rpcs(self):
        msg = self.rpc.cluster_add(
            name='ceph', user='client', key='ABC123', mon_host='1.1.1.1')
        rv = self.msgloop(msg)
        self.assertNotIn("error", rv)

        msg = self.rpc.create(
            nqn='nqn.1', cluster='ceph', pool_name='mypool',
            rbd_name='myimage', addr='0.0.0.0')
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)
        prev = rv

        msg = self.rpc.find(nqn=prev['nqn'])
        rv = self.msgloop(msg)
        self.assertEqual(rv.get('pool'), 'mypool')
        self.assertEqual(rv.get('image'), 'myimage')
        self.assertEqual(rv.get('cluster'), 'ceph')
        self.assertEqual(rv.get('addr'), '0.0.0.0')
        self.assertIn('port', rv)

        msg = self.rpc.join(
            nqn='nqn.1', addresses=[{'addr': '127.0.0.1', 'port': 65001}],
            addr='127.0.0.1')
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)

        msg = self.rpc.host_add(host='host', nqn=prev['nqn'],
                                dhchap_key='some-key')
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)

        msg = self.rpc.host_list(nqn=prev['nqn'])
        rv = self.msgloop(msg)
        self.assertEqual(len(rv), 1)
        self.assertEqual(rv[0]['host'], 'host')
        self.assertEqual(rv[0]['dhchap_key'],
                         prev['nqn'] + '@' + rv[0]['host'])

        msg = self.rpc.host_add(host='any', nqn=prev['nqn'])
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)

        msg = self.rpc.host_list(nqn=prev['nqn'])
        rv = self.msgloop(msg)
        self.assertEqual('any', rv)

        msg = self.rpc.host_del(host='host', nqn=prev['nqn'])
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)

        msg = self.rpc.leave(subsystems=[
            dict(nqn='nqn.1', addr='127.0.0.1', port=65001)])
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)

        rv = self.msgloop(self.rpc.list())
        self.assertEqual(len(rv), 1)
        self.assertEqual(rv[0]['type'], 'rbd')

        msg = self.rpc.remove(nqn=prev['nqn'])
        rv = self.msgloop(msg)
        self.assertNotIn('error', rv)


class MockGmapWithContents(MockGmap):
    BASE = {'subsys':
            {'nqn.1':
                {'name': 'rbd://{"pool":"p1","image":"i1","cluster":"ceph"}',
                 'hosts': [{'host': 'any', 'dhchap_key': False},
                           {'host': 'nqn.2',
                            'dhchap_key': 'some-key'}],
                 'units': {'nx': ['127.0.0.1', '8888']}}}}


class TestProxyWithContents(TestBase):
    GMAP_CLS = MockGmapWithContents
    LOCAL_PORT = 65001
    LOCAL_STATE = {'clusters': [{'name': 'ceph', 'user': 'u1', 'key': 'K',
                                 'mon_host': '1.1.1.1'}]}

    def test_proxy_with_contents(self):
        rv = self.msgloop(self.rpc.list())
        self.assertNotIn('error', 'rv')
        self.assertEqual(len(rv), 1)

        elem = rv[0]
        self.assertEqual('nqn.1', elem['nqn'])
        self.assertEqual('127.0.0.1', elem['addr'])
        self.assertEqual('8888', elem['port'])


class TestProxyEmptyLocalFile(TestBase):
    LOCAL_STATE = ''
    GMAP_CLS = MockGmap
    LOCAL_PORT = 65002
    PRE_CMDS = (('nvmf_create_transport', {'trtype': 'tcp'}),)

    def test_proxy_empty_local_file(self):
        rv = self.msgloop(self.rpc.list())
        self.assertNotIn('error', rv)
        self.assertEqual(len(rv), 0)
