# Copyright 2024 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Encapsulate CephFS testing."""

import logging
import subprocess
import tenacity
from tenacity import retry, Retrying, stop_after_attempt, wait_exponential
import unittest
import zaza
import zaza.model as model
import zaza.openstack.charm_tests.test_utils as test_utils
import zaza.openstack.utilities.ceph as zaza_ceph
import zaza.openstack.utilities.openstack as openstack_utils


class CephFSTests(unittest.TestCase):
    """Encapsulate CephFS tests."""

    mounts_share = False
    mount_dir = '/mnt/cephfs'
    CEPH_MON = 'ceph-mon'

    def tearDown(self):
        """Cleanup after running tests."""
        if self.mounts_share:
            for unit in ['ubuntu/0', 'ubuntu/1']:
                try:
                    zaza.utilities.generic.run_via_ssh(
                        unit_name=unit,
                        cmd='sudo fusermount -u {0} && sudo rmdir {0}'.format(
                            self.mount_dir))
                except subprocess.CalledProcessError:
                    logging.warning(
                        "Failed to cleanup mounts on {}".format(unit))

    def _mount_share(self, unit_name: str,
                     retry: bool = True):
        self._install_dependencies(unit_name)
        self._install_keyring(unit_name)
        ssh_cmd = (
            'sudo mkdir -p {0} && '
            'sudo ceph-fuse {0}'.format(self.mount_dir)
        )
        if retry:
            for attempt in Retrying(
                    stop=stop_after_attempt(5),
                    wait=wait_exponential(multiplier=3,
                                          min=2, max=10)):
                with attempt:
                    zaza.utilities.generic.run_via_ssh(
                        unit_name=unit_name,
                        cmd=ssh_cmd)
        else:
            zaza.utilities.generic.run_via_ssh(
                unit_name=unit_name,
                cmd=ssh_cmd)
        self.mounts_share = True

    def _install_keyring(self, unit_name: str):

        keyring = model.run_on_leader(
            self.CEPH_MON, 'cat /etc/ceph/ceph.client.admin.keyring')['Stdout']
        config = model.run_on_leader(
            self.CEPH_MON, 'cat /etc/ceph/ceph.conf')['Stdout']
        commands = [
            'sudo mkdir -p /etc/ceph',
            "echo '{}' | sudo tee /etc/ceph/ceph.conf".format(config),
            "echo '{}' | "
            'sudo tee /etc/ceph/ceph.client.admin.keyring'.format(keyring)
        ]
        for cmd in commands:
            zaza.utilities.generic.run_via_ssh(
                unit_name=unit_name,
                cmd=cmd)

    def _install_dependencies(self, unit: str):
        zaza.utilities.generic.run_via_ssh(
            unit_name=unit,
            cmd='sudo apt-get install -yq ceph-fuse')

    @classmethod
    def setUpClass(cls):
        """Run class setup for running tests."""
        super(CephFSTests, cls).setUpClass()

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=3, min=2, max=10))
    def _write_testing_file_on_instance(self, instance_name: str):
        zaza.utilities.generic.run_via_ssh(
            unit_name=instance_name,
            cmd='echo -n "test" | sudo tee {}/test'.format(self.mount_dir))

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=3, min=2, max=10))
    def _verify_testing_file_on_instance(self, instance_name: str):
        output = zaza.model.run_on_unit(
            instance_name, 'sudo cat {}/test'.format(self.mount_dir))['Stdout']
        self.assertEqual('test', output.strip())

    def test_cephfs_share(self):
        """Test that CephFS shares can be accessed on two instances.

        1. Spawn two servers
        2. mount it on both
        3. write a file on one
        4. read it on the other
        5. profit
        """
        self._mount_share('ubuntu/0')
        self._mount_share('ubuntu/1')

        self._write_testing_file_on_instance('ubuntu/0')
        self._verify_testing_file_on_instance('ubuntu/1')


class CharmOperationTest(test_utils.BaseCharmTest):
    """CephFS Charm operation tests."""

    def test_pause_resume(self):
        """Run pause and resume tests.

        Pause service and check services are stopped, then resume and check
        they are started.
        """
        services = ['ceph-mds']
        with self.pause_resume(services):
            logging.info('Testing pause resume (services="{}")'
                         .format(services))


class BlueStoreCompressionCharmOperation(test_utils.BaseCharmTest):
    """Test charm handling of bluestore compression configuration options."""

    @classmethod
    def setUpClass(cls):
        """Perform class one time initialization."""
        super(BlueStoreCompressionCharmOperation, cls).setUpClass()
        release_application = 'keystone'
        try:
            model.get_application(release_application)
        except KeyError:
            release_application = 'ceph-mon'
        cls.current_release = openstack_utils.get_os_release(
            application=release_application)
        cls.bionic_rocky = openstack_utils.get_os_release('bionic_rocky')

    def setUp(self):
        """Perform common per test initialization steps."""
        super(BlueStoreCompressionCharmOperation, self).setUp()

        # determine if the tests should be run or not
        logging.debug('os_release: {} >= {} = {}'
                      .format(self.current_release,
                              self.bionic_rocky,
                              self.current_release >= self.bionic_rocky))
        self.mimic_or_newer = self.current_release >= self.bionic_rocky

    def _assert_pools_properties(self, pools, pools_detail,
                                 expected_properties, log_func=logging.info):
        """Check properties on a set of pools.

        :param pools: List of pool names to check.
        :type pools: List[str]
        :param pools_detail: List of dictionaries with pool detail
        :type pools_detail List[Dict[str,any]]
        :param expected_properties: Properties to check and their expected
                                    values.
        :type expected_properties: Dict[str,any]
        :returns: Nothing
        :raises: AssertionError
        """
        for pool in pools:
            for pd in pools_detail:
                if pd['pool_name'] == pool:
                    if 'options' in expected_properties:
                        for k, v in expected_properties['options'].items():
                            self.assertEquals(pd['options'][k], v)
                            log_func("['options']['{}'] == {}".format(k, v))
                    for k, v in expected_properties.items():
                        if k == 'options':
                            continue
                        self.assertEquals(pd[k], v)
                        log_func("{} == {}".format(k, v))

    def test_configure_compression(self):
        """Enable compression and validate properties flush through to pool."""
        if not self.mimic_or_newer:
            logging.info('Skipping test, Mimic or newer required.')
            return
        if self.application_name == 'ceph-osd':
            # The ceph-osd charm itself does not request pools, neither does
            # the BlueStore Compression configuration options it have affect
            # pool properties.
            logging.info('test does not apply to ceph-osd charm.')
            return
        elif self.application_name == 'ceph-radosgw':
            # The Ceph RadosGW creates many light weight pools to keep track of
            # metadata, we only compress the pool containing actual data.
            app_pools = ['.rgw.buckets.data']
        else:
            # Retrieve which pools the charm under test has requested skipping
            # metadata pools as they are deliberately not compressed.
            app_pools = [
                pool
                for pool in zaza_ceph.get_pools_from_broker_req(
                    self.application_name, model_name=self.model_name)
                if 'metadata' not in pool
            ]

        ceph_pools_detail = zaza_ceph.get_ceph_pool_details(
            model_name=self.model_name)

        logging.debug('BEFORE: {}'.format(ceph_pools_detail))
        try:
            logging.info('Checking Ceph pool compression_mode prior to change')
            self._assert_pools_properties(
                app_pools, ceph_pools_detail,
                {'options': {'compression_mode': 'none'}})
        except KeyError:
            logging.info('property does not exist on pool, which is OK.')
        logging.info('Changing "bluestore-compression-mode" to "force" on {}'
                     .format(self.application_name))
        with self.config_change(
                {'bluestore-compression-mode': 'none'},
                {'bluestore-compression-mode': 'force'}):
            logging.info('Checking Ceph pool compression_mode after to change')
            self._check_pool_compression_mode(app_pools, 'force')

        logging.info('Checking Ceph pool compression_mode after '
                     'restoring config to previous value')
        self._check_pool_compression_mode(app_pools, 'none')

    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=2, max=10),
        stop=tenacity.stop_after_attempt(10),
        reraise=True,
        retry=tenacity.retry_if_exception_type(AssertionError)
    )
    def _check_pool_compression_mode(self, app_pools, mode):
        ceph_pools_detail = zaza_ceph.get_ceph_pool_details(
            model_name=self.model_name)
        logging.debug('ceph_pools_details: %s', ceph_pools_detail)
        self._assert_pools_properties(
            app_pools, ceph_pools_detail,
            {'options': {'compression_mode': mode}})

    def test_invalid_compression_configuration(self):
        """Set invalid configuration and validate charm response."""
        if not self.mimic_or_newer:
            logging.info('Skipping test, Mimic or newer required.')
            return
        stored_target_deploy_status = self.test_config.get(
            'target_deploy_status', {})
        new_target_deploy_status = stored_target_deploy_status.copy()
        new_target_deploy_status[self.application_name] = {
            'workload-status': 'blocked',
            'workload-status-message': 'Invalid configuration',
        }
        if 'target_deploy_status' in self.test_config:
            self.test_config['target_deploy_status'].update(
                new_target_deploy_status)
        else:
            self.test_config['target_deploy_status'] = new_target_deploy_status

        with self.config_change(
                {'bluestore-compression-mode': 'none'},
                {'bluestore-compression-mode': 'PEBCAK'}):
            logging.info('Charm went into blocked state as expected, restore '
                         'configuration')
            self.test_config[
                'target_deploy_status'] = stored_target_deploy_status


def setup_osd_standalone():
    """Perform the necessary steps to setup a single OSD."""
    cmds = ['sudo ceph osd crush rule rm replicated_rule',
            'sudo ceph osd crush rule create-replicated replicated_rule '
            'default osd',
            'sudo ceph osd erasure-code-profile rm default',
            'sudo ceph osd erasure-code-profile set default '
            'plugin=jerasure k=2 m=1 crush-failure-domain=osd']
    for cmd in cmds:
        model.run_on_unit('ceph-mon/0', cmd)

    loops = []
    for file in ('l1', 'l2', 'l3'):
        model.run_on_unit('ceph-osd/0', 'touch %s' % file)
        model.run_on_unit('ceph-osd/0', 'truncate --size 2G ./%s' % file)
        out = model.run_on_unit('ceph-osd/0',
                                'sudo losetup -fP --show ./%s' % file)
        loops.append(out['Stdout'].strip())

    for loop in loops:
        model.run_action_on_leader('ceph-osd', 'add-disk',
                                   action_params={'osd-devices': loop})

    states = None
    try:
        model.get_application('ubuntu')
        states = {'ubuntu': {'workload-status-message': ''}}
    except KeyError:
        pass

    model.wait_for_application_states(states=states)
