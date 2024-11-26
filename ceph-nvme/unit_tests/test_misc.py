import logging
import unittest
import unittest.mock as mock

import src.utils as src_utils
import src.radosmap as radosmap


class MockRados:
    THROW = False

    def __init__(self, *args, **kwargs):
        self.connect = mock.MagicMock()
        if self.THROW:
            def _throw(*_args, **_kwargs):
                raise RuntimeError()
            self.open_ioctx = _throw
        else:
            self.open_ioctx = mock.MagicMock()

        self.shutdown = mock.MagicMock()


class MockIoctx:
    def __init__(self, result):
        self.result = result

    def read(self, name, length):
        if not isinstance(self.result, bytes):
            raise self.result()

        return self.result


class WriteOp:
    def __init__(self):
        self.new = mock.MagicMock()
        self.assert_version = mock.MagicMock()
        self.write_full = mock.MagicMock()

    def release(self):
        pass


class RadosObjects:
    ObjectNotFound = KeyError
    ObjectExists = ValueError
    OSError = OSError
    Rados = MockRados


class TestRadosMap(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger(__name__)
        radosmap.rados = RadosObjects

    def test_rados_connect(self):
        rd = radosmap.RadosMap('some-pool', self.logger)
        MockRados.THROW = False
        rd.add_cluster('ceph-nvme', 'some-key', '0.0.0.0')
        rd.cluster.connect.assert_called_once()
        rd.cluster.open_ioctx.assert_called_once()

    def test_rados_connfailed(self):
        rd = radosmap.RadosMap('some-pool', self.logger)
        MockRados.THROW = True
        with self.assertRaises(Exception):
            rd.add_cluster('ceph-nvme', 'some-key', '0.0.0.0')

    def test_global_map(self):
        rd = radosmap.RadosMap('some-pool', self.logger)
        rd.ioctx = MockIoctx(b'{"version":%d,"subsys":{}}' % radosmap.VERSION)
        ret = rd.get_global_map()
        self.assertNotEqual(ret['version'], 0)

    def test_global_map_nonexistent(self):
        rd = radosmap.RadosMap('some-pool', self.logger)
        rd.ioctx = MockIoctx(radosmap.rados.ObjectNotFound)
        ret = rd.get_global_map()
        self.assertEqual(ret['version'], 0)

    def test_global_map_fail(self):
        rd = radosmap.RadosMap('some-pool', self.logger)
        rd.ioctx = MockIoctx(b'{')
        with self.assertRaises(RuntimeError):
            rd.get_global_map()

    def test_update_map_first(self):
        rd = radosmap.RadosMap('some-pool', self.logger)
        rd.get_global_map = lambda: {'version': 0, 'subsys': {}}
        rd.ioctx = mock.MagicMock()
        rd.ioctx.operate_write_op = mock.MagicMock()
        wop = WriteOp()
        rd.ioctx.create_write_op = lambda: wop

        def _update(gmap):
            gmap['subsys']['nqn.1'] = {}

        rd.update_map(_update)
        rd.ioctx.operate_write_op.assert_called_with(wop, 'global-map')
        wop.write_full.assert_called_with(
            radosmap.json.dumps({'version': radosmap.VERSION,
                                 'subsys': {'nqn.1': {}}}).encode('utf8'))


class TestUtils(unittest.TestCase):
    def test_utils(self):
        rpc = src_utils.RPC()
        payload = rpc.method_1()
        self.assertNotIn('params', payload)
        payload = rpc.method_2(param=1)
        self.assertIn('params', payload)

    @mock.patch.object(src_utils.os, 'sched_getaffinity')
    def test_cpu_utils(self, get_affinity):
        cpus = [0, 1, 2, 3, 4]
        get_affinity.return_value = cpus
        cpuset = src_utils.default_cpuset(cpus)
        self.assertTrue(len(cpuset) >= len(cpus) // 2)

        cpuset = src_utils.compute_cpuset('[1,33]')
        self.assertEqual(cpuset, [1])

        cpuset = src_utils.compute_cpuset('3')
        self.assertEqual(len(cpuset), 3)

        mask = src_utils.compute_cpumask([3])
        self.assertEqual(0x8, mask)

    def test_sock_utils(self):
        with self.assertRaises(OSError):
            src_utils.get_adrfam('???')

        port, fam = src_utils.get_free_port('127.0.0.1')
        self.assertLess(port, 0xffff)
        self.assertEqual(fam, 'IPv4')
        port, fam = src_utils.get_free_port('::1')
        self.assertLess(port, 0xffff)
        self.assertEqual(fam, 'IPv6')

        xaddr = src_utils.get_external_addr()
        _, fam = src_utils.get_adrfam(xaddr)
        self.assertTrue(fam == 'IPv4' or fam == 'IPv6')
