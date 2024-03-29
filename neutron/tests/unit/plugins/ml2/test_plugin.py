# Copyright (c) 2013 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import functools

import fixtures
import mock
import six
import testtools
import webob

from neutron_lib import constants
from neutron_lib import exceptions as exc
from neutron_lib.plugins import directory
from oslo_db import exception as db_exc
from oslo_utils import uuidutils
from sqlalchemy.orm import exc as sqla_exc

from neutron._i18n import _
from neutron.callbacks import events
from neutron.callbacks import exceptions as c_exc
from neutron.callbacks import registry
from neutron.callbacks import resources
from neutron.common import utils
from neutron import context
from neutron.db import agents_db
from neutron.db import api as db_api
from neutron.db import db_base_plugin_v2 as base_plugin
from neutron.db.models import l3 as l3_models
from neutron.db import models_v2
from neutron.db import provisioning_blocks
from neutron.db import segments_db
from neutron.extensions import availability_zone as az_ext
from neutron.extensions import external_net
from neutron.extensions import multiprovidernet as mpnet
from neutron.extensions import portbindings
from neutron.extensions import providernet as pnet
from neutron.plugins.common import constants as p_const
from neutron.plugins.ml2.common import exceptions as ml2_exc
from neutron.plugins.ml2 import config
from neutron.plugins.ml2 import db as ml2_db
from neutron.plugins.ml2 import driver_api
from neutron.plugins.ml2 import driver_context
from neutron.plugins.ml2.drivers import type_vlan
from neutron.plugins.ml2 import managers
from neutron.plugins.ml2 import models
from neutron.plugins.ml2 import plugin as ml2_plugin
from neutron.services.l3_router import l3_router_plugin
from neutron.services.revisions import revision_plugin
from neutron.services.segments import db as segments_plugin_db
from neutron.services.segments import plugin as segments_plugin
from neutron.tests import base
from neutron.tests.common import helpers
from neutron.tests.unit import _test_extension_portbindings as test_bindings
from neutron.tests.unit.agent import test_securitygroups_rpc as test_sg_rpc
from neutron.tests.unit.db import test_allowedaddresspairs_db as test_pair
from neutron.tests.unit.db import test_db_base_plugin_v2 as test_plugin
from neutron.tests.unit.db import test_ipam_pluggable_backend as test_ipam
from neutron.tests.unit.extensions import test_extra_dhcp_opt as test_dhcpopts
from neutron.tests.unit.plugins.ml2.drivers import mechanism_logger as \
     mech_logger
from neutron.tests.unit.plugins.ml2.drivers import mechanism_test as mech_test


config.cfg.CONF.import_opt('network_vlan_ranges',
                           'neutron.plugins.ml2.drivers.type_vlan',
                           group='ml2_type_vlan')


PLUGIN_NAME = 'ml2'

DEVICE_OWNER_COMPUTE = constants.DEVICE_OWNER_COMPUTE_PREFIX + 'fake'
HOST = 'fake_host'
TEST_ROUTER_ID = 'router_id'


# TODO(marun) - Move to somewhere common for reuse
class PluginConfFixture(fixtures.Fixture):
    """Plugin configuration shared across the unit and functional tests."""

    def __init__(self, plugin_name, parent_setup=None):
        super(PluginConfFixture, self).__init__()
        self.plugin_name = plugin_name
        self.parent_setup = parent_setup

    def _setUp(self):
        if self.parent_setup:
            self.parent_setup()


class Ml2ConfFixture(PluginConfFixture):

    def __init__(self, parent_setup=None):
        super(Ml2ConfFixture, self).__init__(PLUGIN_NAME, parent_setup)


class Ml2PluginV2TestCase(test_plugin.NeutronDbPluginV2TestCase):

    _mechanism_drivers = ['logger', 'test']
    l3_plugin = ('neutron.tests.unit.extensions.test_l3.'
                 'TestL3NatServicePlugin')

    def get_additional_service_plugins(self):
        """Subclasses can return a dictionary of service plugins to load."""
        return {}

    def setup_parent(self):
        """Perform parent setup with the common plugin configuration class."""
        service_plugins = {'l3_plugin_name': self.l3_plugin}
        service_plugins.update(self.get_additional_service_plugins())
        # Ensure that the parent setup can be called without arguments
        # by the common configuration setUp.
        parent_setup = functools.partial(
            super(Ml2PluginV2TestCase, self).setUp,
            plugin=PLUGIN_NAME,
            service_plugins=service_plugins,
        )
        self.useFixture(Ml2ConfFixture(parent_setup))
        self.port_create_status = 'DOWN'

    def setUp(self):
        self.ovo_push_interface_p = mock.patch(
            'neutron.plugins.ml2.ovo_rpc.OVOServerRpcInterface')
        self.ovo_push_interface_p.start()
        # Enable the test mechanism driver to ensure that
        # we can successfully call through to all mechanism
        # driver apis.
        config.cfg.CONF.set_override('mechanism_drivers',
                                     self._mechanism_drivers,
                                     group='ml2')
        self.physnet = 'physnet1'
        self.vlan_range = '1:100'
        self.vlan_range2 = '200:300'
        self.physnet2 = 'physnet2'
        self.phys_vrange = ':'.join([self.physnet, self.vlan_range])
        self.phys2_vrange = ':'.join([self.physnet2, self.vlan_range2])
        config.cfg.CONF.set_override('network_vlan_ranges',
                                     [self.phys_vrange, self.phys2_vrange],
                                     group='ml2_type_vlan')
        self.setup_parent()
        self.driver = directory.get_plugin()
        self.context = context.get_admin_context()


class TestMl2BulkToggleWithoutBulkless(Ml2PluginV2TestCase):

    _mechanism_drivers = ['logger', 'test']

    def test_bulk_enabled_with_bulk_drivers(self):
        self.assertFalse(self._skip_native_bulk)


class TestMl2BasicGet(test_plugin.TestBasicGet,
                      Ml2PluginV2TestCase):
    pass


class TestMl2V2HTTPResponse(test_plugin.TestV2HTTPResponse,
                            Ml2PluginV2TestCase):
    pass


class TestMl2NetworksV2(test_plugin.TestNetworksV2,
                        Ml2PluginV2TestCase):
    def setUp(self, plugin=None):
        super(TestMl2NetworksV2, self).setUp()
        # provider networks
        self.pnets = [{'name': 'net1',
                       pnet.NETWORK_TYPE: 'vlan',
                       pnet.PHYSICAL_NETWORK: 'physnet1',
                       pnet.SEGMENTATION_ID: 1,
                       'tenant_id': 'tenant_one'},
                      {'name': 'net2',
                       pnet.NETWORK_TYPE: 'vlan',
                       pnet.PHYSICAL_NETWORK: 'physnet2',
                       pnet.SEGMENTATION_ID: 210,
                       'tenant_id': 'tenant_one'},
                      {'name': 'net3',
                       pnet.NETWORK_TYPE: 'vlan',
                       pnet.PHYSICAL_NETWORK: 'physnet2',
                       pnet.SEGMENTATION_ID: 220,
                       'tenant_id': 'tenant_one'}
                      ]
        # multiprovider networks
        self.mp_nets = [{'name': 'net4',
                         mpnet.SEGMENTS:
                             [{pnet.NETWORK_TYPE: 'vlan',
                               pnet.PHYSICAL_NETWORK: 'physnet2',
                               pnet.SEGMENTATION_ID: 1},
                              {pnet.NETWORK_TYPE: 'vlan',
                               pnet.PHYSICAL_NETWORK: 'physnet2',
                               pnet.SEGMENTATION_ID: 202}],
                         'tenant_id': 'tenant_one'}
                        ]
        self.nets = self.mp_nets + self.pnets

    def test_network_after_create_callback(self):
        after_create = mock.Mock()
        registry.subscribe(after_create, resources.NETWORK,
                           events.AFTER_CREATE)
        with self.network() as n:
            after_create.assert_called_once_with(
                resources.NETWORK, events.AFTER_CREATE, mock.ANY,
                context=mock.ANY, network=mock.ANY)
            kwargs = after_create.mock_calls[0][2]
            self.assertEqual(n['network']['id'],
                             kwargs['network']['id'])

    def test_network_after_update_callback(self):
        after_update = mock.Mock()
        registry.subscribe(after_update, resources.NETWORK,
                           events.AFTER_UPDATE)
        with self.network() as n:
            data = {'network': {'name': 'updated'}}
            req = self.new_update_request('networks', data, n['network']['id'])
            self.deserialize(self.fmt, req.get_response(self.api))
            after_update.assert_called_once_with(
                resources.NETWORK, events.AFTER_UPDATE, mock.ANY,
                context=mock.ANY, network=mock.ANY, original_network=mock.ANY)
            kwargs = after_update.mock_calls[0][2]
            self.assertEqual(n['network']['name'],
                             kwargs['original_network']['name'])
            self.assertEqual('updated', kwargs['network']['name'])

    def test_network_after_delete_callback(self):
        after_delete = mock.Mock()
        registry.subscribe(after_delete, resources.NETWORK,
                           events.AFTER_DELETE)
        with self.network() as n:
            req = self.new_delete_request('networks', n['network']['id'])
            req.get_response(self.api)
            after_delete.assert_called_once_with(
                resources.NETWORK, events.AFTER_DELETE, mock.ANY,
                context=mock.ANY, network=mock.ANY)
            kwargs = after_delete.mock_calls[0][2]
            self.assertEqual(n['network']['id'],
                             kwargs['network']['id'])

    def test_port_delete_helper_tolerates_failure(self):
        plugin = directory.get_plugin()
        with mock.patch.object(plugin, "delete_port",
                               side_effect=exc.PortNotFound(port_id="123")):
            plugin._delete_ports(mock.MagicMock(), [mock.MagicMock()])

        with mock.patch.object(plugin, "delete_port",
                               side_effect=sqla_exc.ObjectDeletedError(None)):
            plugin._delete_ports(mock.MagicMock(), [mock.MagicMock()])

    def test_subnet_delete_helper_tolerates_failure(self):
        plugin = directory.get_plugin()
        with mock.patch.object(plugin, "delete_subnet",
                               side_effect=exc.SubnetNotFound(subnet_id="1")):
            plugin._delete_subnets(mock.MagicMock(), [mock.MagicMock()])

        with mock.patch.object(plugin, "delete_subnet",
                               side_effect=sqla_exc.ObjectDeletedError(None)):
            plugin._delete_subnets(mock.MagicMock(), [mock.MagicMock()])

    def _create_and_verify_networks(self, networks):
        for net_idx, net in enumerate(networks):
            # create
            req = self.new_create_request('networks',
                                          {'network': net})
            # verify
            network = self.deserialize(self.fmt,
                                       req.get_response(self.api))['network']
            if mpnet.SEGMENTS not in net:
                for k, v in six.iteritems(net):
                    self.assertEqual(net[k], network[k])
                    self.assertNotIn(mpnet.SEGMENTS, network)
            else:
                segments = network[mpnet.SEGMENTS]
                expected_segments = net[mpnet.SEGMENTS]
                self.assertEqual(len(expected_segments), len(segments))
                for expected, actual in zip(expected_segments, segments):
                    self.assertEqual(expected, actual)

    def _lookup_network_by_segmentation_id(self, seg_id, num_expected_nets):
        params_str = "%s=%s" % (pnet.SEGMENTATION_ID, seg_id)
        net_req = self.new_list_request('networks', None,
                                        params=params_str)
        networks = self.deserialize(self.fmt, net_req.get_response(self.api))
        if num_expected_nets:
            self.assertIsNotNone(networks)
            self.assertEqual(num_expected_nets, len(networks['networks']))
        else:
            self.assertIsNone(networks)
        return networks

    def test_list_networks_with_segmentation_id(self):
        self._create_and_verify_networks(self.pnets)
        # verify we can find the network that we expect
        lookup_vlan_id = 1
        expected_net = [n for n in self.pnets
                        if n[pnet.SEGMENTATION_ID] == lookup_vlan_id].pop()
        networks = self._lookup_network_by_segmentation_id(lookup_vlan_id, 1)
        # verify all provider attributes
        network = networks['networks'][0]
        for attr in pnet.ATTRIBUTES:
            self.assertEqual(expected_net[attr], network[attr])

    def test_list_mpnetworks_with_segmentation_id(self):
        self._create_and_verify_networks(self.nets)

        # get all networks with seg_id=1 (including multisegment networks)
        lookup_vlan_id = 1
        networks = self._lookup_network_by_segmentation_id(lookup_vlan_id, 2)

        # get the mpnet
        networks = [n for n in networks['networks'] if mpnet.SEGMENTS in n]
        network = networks.pop()
        # verify attributes of the looked up item
        segments = network[mpnet.SEGMENTS]
        expected_segments = self.mp_nets[0][mpnet.SEGMENTS]
        self.assertEqual(len(expected_segments), len(segments))
        for expected, actual in zip(expected_segments, segments):
            self.assertEqual(expected, actual)

    def test_create_network_segment_allocation_fails(self):
        plugin = directory.get_plugin()
        mock.patch.object(db_api._retry_db_errors, 'max_retries',
                          new=2).start()
        with mock.patch.object(
            plugin.type_manager, 'create_network_segments',
            side_effect=db_exc.RetryRequest(ValueError())
        ) as f:
            data = {'network': {'tenant_id': 'sometenant', 'name': 'dummy',
                                'admin_state_up': True, 'shared': False}}
            req = self.new_create_request('networks', data)
            res = req.get_response(self.api)
            self.assertEqual(500, res.status_int)
            # 1 + retry count
            self.assertEqual(3, f.call_count)


class TestExternalNetwork(Ml2PluginV2TestCase):

    def _create_external_network(self):
        data = {'network': {'name': 'net1',
                            'router:external': 'True',
                            'tenant_id': 'tenant_one'}}
        network_req = self.new_create_request('networks', data)
        network = self.deserialize(self.fmt,
                                   network_req.get_response(self.api))
        return network

    def test_external_network_type_none(self):
        config.cfg.CONF.set_default('external_network_type',
                                    None,
                                    group='ml2')

        network = self._create_external_network()
        # For external network, expected network type to be
        # tenant_network_types which is by default 'local'.
        self.assertEqual(p_const.TYPE_LOCAL,
                         network['network'][pnet.NETWORK_TYPE])
        # No physical network specified, expected 'None'.
        self.assertIsNone(network['network'][pnet.PHYSICAL_NETWORK])
        # External network will not have a segmentation id.
        self.assertIsNone(network['network'][pnet.SEGMENTATION_ID])
        # External network will not have multiple segments.
        self.assertNotIn(mpnet.SEGMENTS, network['network'])

    def test_external_network_type_vlan(self):
        config.cfg.CONF.set_default('external_network_type',
                                    p_const.TYPE_VLAN,
                                    group='ml2')

        network = self._create_external_network()
        # For external network, expected network type to be 'vlan'.
        self.assertEqual(p_const.TYPE_VLAN,
                         network['network'][pnet.NETWORK_TYPE])
        # Physical network is expected.
        self.assertIsNotNone(network['network'][pnet.PHYSICAL_NETWORK])
        # External network will have a segmentation id.
        self.assertIsNotNone(network['network'][pnet.SEGMENTATION_ID])
        # External network will not have multiple segments.
        self.assertNotIn(mpnet.SEGMENTS, network['network'])


class TestMl2NetworksWithVlanTransparencyBase(TestMl2NetworksV2):
    data = {'network': {'name': 'net1',
                        mpnet.SEGMENTS:
                        [{pnet.NETWORK_TYPE: 'vlan',
                          pnet.PHYSICAL_NETWORK: 'physnet1'}],
                        'tenant_id': 'tenant_one',
                        'vlan_transparent': 'True'}}

    def setUp(self, plugin=None):
        config.cfg.CONF.set_override('vlan_transparent', True)
        super(TestMl2NetworksWithVlanTransparencyBase, self).setUp(plugin)


class TestMl2NetworksWithVlanTransparency(
    TestMl2NetworksWithVlanTransparencyBase):
    _mechanism_drivers = ['test']

    def test_create_network_vlan_transparent_fail(self):
        with mock.patch.object(mech_test.TestMechanismDriver,
                               'check_vlan_transparency',
                               return_value=False):
            network_req = self.new_create_request('networks', self.data)
            res = network_req.get_response(self.api)
            self.assertEqual(500, res.status_int)
            error_result = self.deserialize(self.fmt, res)['NeutronError']
            self.assertEqual("VlanTransparencyDriverError",
                             error_result['type'])

    def test_create_network_vlan_transparent(self):
        with mock.patch.object(mech_test.TestMechanismDriver,
                               'check_vlan_transparency',
                               return_value=True):
            network_req = self.new_create_request('networks', self.data)
            res = network_req.get_response(self.api)
            self.assertEqual(201, res.status_int)
            network = self.deserialize(self.fmt, res)['network']
            self.assertIn('vlan_transparent', network)


class TestMl2NetworksWithVlanTransparencyAndMTU(
    TestMl2NetworksWithVlanTransparencyBase):
    _mechanism_drivers = ['test']

    def test_create_network_vlan_transparent_and_mtu(self):
        with mock.patch.object(mech_test.TestMechanismDriver,
                               'check_vlan_transparency',
                               return_value=True):
            config.cfg.CONF.set_override('path_mtu', 1000, group='ml2')
            config.cfg.CONF.set_override('global_physnet_mtu', 1000)
            network_req = self.new_create_request('networks', self.data)
            res = network_req.get_response(self.api)
            self.assertEqual(201, res.status_int)
            network = self.deserialize(self.fmt, res)['network']
            self.assertEqual(1000, network['mtu'])
            self.assertIn('vlan_transparent', network)
            self.assertTrue(network['vlan_transparent'])
        self.assertTrue(network['vlan_transparent'])


class TestMl2NetworksWithAvailabilityZone(TestMl2NetworksV2):
    def test_create_network_availability_zone(self):
        az_hints = ['az1', 'az2']
        data = {'network': {'name': 'net1',
                            az_ext.AZ_HINTS: az_hints,
                            'tenant_id': 'tenant_one'}}
        with mock.patch.object(agents_db.AgentAvailabilityZoneMixin,
                               'validate_availability_zones'):
            network_req = self.new_create_request('networks', data)
            res = network_req.get_response(self.api)
            self.assertEqual(201, res.status_int)
            network = self.deserialize(self.fmt, res)['network']
            self.assertEqual(az_hints, network[az_ext.AZ_HINTS])


class TestMl2SubnetsV2(test_plugin.TestSubnetsV2,
                       Ml2PluginV2TestCase):

    def test_subnet_after_create_callback(self):
        after_create = mock.Mock()
        registry.subscribe(after_create, resources.SUBNET, events.AFTER_CREATE)
        with self.subnet() as s:
            after_create.assert_called_once_with(
                resources.SUBNET, events.AFTER_CREATE, mock.ANY,
                context=mock.ANY, subnet=mock.ANY)
            kwargs = after_create.mock_calls[0][2]
            self.assertEqual(s['subnet']['id'], kwargs['subnet']['id'])

    def test_port_update_subnetnotfound(self):
        with self.network() as n:
            with self.subnet(network=n, cidr='1.1.1.0/24') as s1,\
                    self.subnet(network=n, cidr='1.1.2.0/24') as s2,\
                    self.subnet(network=n, cidr='1.1.3.0/24') as s3:
                fixed_ips = [{'subnet_id': s1['subnet']['id']},
                             {'subnet_id': s2['subnet']['id']},
                             {'subnet_id': s3['subnet']['id']}]
                with self.port(subnet=s1, fixed_ips=fixed_ips,
                               device_owner=constants.DEVICE_OWNER_DHCP) as p:
                    plugin = directory.get_plugin()
                    orig_update = plugin.update_port

                    def delete_before_update(ctx, *args, **kwargs):
                        # swap back out with original so only called once
                        plugin.update_port = orig_update
                        # delete s2 in the middle of s1 port_update
                        plugin.delete_subnet(ctx, s2['subnet']['id'])
                        return plugin.update_port(ctx, *args, **kwargs)
                    plugin.update_port = delete_before_update
                    req = self.new_delete_request('subnets',
                                                  s1['subnet']['id'])
                    res = req.get_response(self.api)
                    self.assertEqual(204, res.status_int)
                    # ensure port only has 1 IP on s3
                    port = self._show('ports', p['port']['id'])['port']
                    self.assertEqual(1, len(port['fixed_ips']))
                    self.assertEqual(s3['subnet']['id'],
                                     port['fixed_ips'][0]['subnet_id'])

    def test_subnet_after_update_callback(self):
        after_update = mock.Mock()
        registry.subscribe(after_update, resources.SUBNET, events.AFTER_UPDATE)
        with self.subnet() as s:
            data = {'subnet': {'name': 'updated'}}
            req = self.new_update_request('subnets', data, s['subnet']['id'])
            self.deserialize(self.fmt, req.get_response(self.api))
            after_update.assert_called_once_with(
                resources.SUBNET, events.AFTER_UPDATE, mock.ANY,
                context=mock.ANY, subnet=mock.ANY,
                original_subnet=mock.ANY)
            kwargs = after_update.mock_calls[0][2]
            self.assertEqual(s['subnet']['name'],
                             kwargs['original_subnet']['name'])
            self.assertEqual('updated', kwargs['subnet']['name'])

    def test_subnet_after_delete_callback(self):
        after_delete = mock.Mock()
        registry.subscribe(after_delete, resources.SUBNET, events.AFTER_DELETE)
        with self.subnet() as s:
            req = self.new_delete_request('subnets', s['subnet']['id'])
            req.get_response(self.api)
            after_delete.assert_called_once_with(
                resources.SUBNET, events.AFTER_DELETE, mock.ANY,
                context=mock.ANY, subnet=mock.ANY)
            kwargs = after_delete.mock_calls[0][2]
            self.assertEqual(s['subnet']['id'], kwargs['subnet']['id'])

    def test_delete_subnet_race_with_dhcp_port_creation(self):
        with self.network() as network:
            with self.subnet(network=network) as subnet:
                subnet_id = subnet['subnet']['id']
                attempt = [0]

                def create_dhcp_port(*args, **kwargs):
                    """A method to emulate race condition.

                    Adds dhcp port in the middle of subnet delete
                    """
                    if attempt[0] > 0:
                        return False
                    attempt[0] += 1
                    data = {'port': {'network_id': network['network']['id'],
                                     'tenant_id':
                                     network['network']['tenant_id'],
                                     'name': 'port1',
                                     'admin_state_up': 1,
                                     'device_owner':
                                     constants.DEVICE_OWNER_DHCP,
                                     'fixed_ips': [{'subnet_id': subnet_id}]}}
                    port_req = self.new_create_request('ports', data)
                    port_res = port_req.get_response(self.api)
                    self.assertEqual(201, port_res.status_int)

                # we mock _subnet_check_ip_allocations with method
                # that creates DHCP port 'in the middle' of subnet_delete
                # causing retry this way subnet is deleted on the
                # second attempt
                registry.subscribe(create_dhcp_port, resources.SUBNET,
                                   events.PRECOMMIT_DELETE)
                req = self.new_delete_request('subnets', subnet_id)
                res = req.get_response(self.api)
                self.assertEqual(204, res.status_int)
                self.assertEqual(1, attempt[0])

    def test_create_subnet_check_mtu_in_mech_context(self):
        plugin = directory.get_plugin()
        plugin.mechanism_manager.create_subnet_precommit = mock.Mock()
        net_arg = {pnet.NETWORK_TYPE: 'vxlan',
                   pnet.SEGMENTATION_ID: '1'}
        network = self._make_network(self.fmt, 'net1', True,
                                     arg_list=(pnet.NETWORK_TYPE,
                                               pnet.SEGMENTATION_ID,),
                                     **net_arg)
        with self.subnet(network=network):
            mock_subnet_pre = plugin.mechanism_manager.create_subnet_precommit
            observerd_mech_context = mock_subnet_pre.call_args_list[0][0][0]
            self.assertEqual(network['network']['mtu'],
                             observerd_mech_context.network.current['mtu'])


class TestMl2DbOperationBounds(test_plugin.DbOperationBoundMixin,
                               Ml2PluginV2TestCase):
    """Test cases to assert constant query count for list operations.

    These test cases assert that an increase in the number of objects
    does not result in an increase of the number of db operations. All
    database lookups during a list operation should be performed in bulk
    so the number of queries required for 2 objects instead of 1 should
    stay the same.
    """

    def setUp(self):
        super(TestMl2DbOperationBounds, self).setUp()
        self.kwargs = self.get_api_kwargs()

    def make_network(self):
        return self._make_network(self.fmt, 'name', True, **self.kwargs)

    def make_subnet(self):
        net = self.make_network()
        setattr(self, '_subnet_count', getattr(self, '_subnet_count', 0) + 1)
        cidr = '1.%s.0.0/24' % self._subnet_count
        return self._make_subnet(self.fmt, net, None, cidr, **self.kwargs)

    def make_port(self):
        net = self.make_network()
        return self._make_port(self.fmt, net['network']['id'], **self.kwargs)

    def test_network_list_queries_constant(self):
        self._assert_object_list_queries_constant(self.make_network,
                                                  'networks')

    def test_subnet_list_queries_constant(self):
        self._assert_object_list_queries_constant(self.make_subnet, 'subnets')

    def test_port_list_queries_constant(self):
        self._assert_object_list_queries_constant(self.make_port, 'ports')
        self._assert_object_list_queries_constant(self.make_port, 'ports',
                                                  filters=['device_id'])
        self._assert_object_list_queries_constant(self.make_port, 'ports',
                                                  filters=['device_id',
                                                           'device_owner'])
        self._assert_object_list_queries_constant(self.make_port, 'ports',
                                                  filters=['tenant_id',
                                                           'name',
                                                           'device_id'])


class TestMl2DbOperationBoundsTenant(TestMl2DbOperationBounds):
    admin = False


class TestMl2DbOperationBoundsTenantRbac(TestMl2DbOperationBoundsTenant):

    def make_port_in_shared_network(self):
        context_ = self._get_context()
        # create shared network owned by the tenant; we use direct driver call
        # because default policy does not allow users to create shared networks
        net = self.driver.create_network(
            context.get_admin_context(),
            {'network': {'name': 'net1',
                         'tenant_id': context_.tenant,
                         'admin_state_up': True,
                         'shared': True}})
        # create port that belongs to another tenant
        return self._make_port(
            self.fmt, net['id'],
            set_context=True, tenant_id='fake_tenant')

    def test_port_list_in_shared_network_queries_constant(self):
        self._assert_object_list_queries_constant(
            self.make_port_in_shared_network, 'ports')


class TestMl2PortsV2(test_plugin.TestPortsV2, Ml2PluginV2TestCase):

    def test__port_provisioned_with_blocks(self):
        plugin = directory.get_plugin()
        ups = mock.patch.object(plugin, 'update_port_status').start()
        with self.port() as port:
            mock.patch('neutron.plugins.ml2.plugin.db.get_port').start()
            provisioning_blocks.add_provisioning_component(
                self.context, port['port']['id'], 'port', 'DHCP')
            plugin._port_provisioned('port', 'evt', 'trigger',
                                     self.context, port['port']['id'])
        self.assertFalse(ups.called)

    def test__port_provisioned_no_binding(self):
        plugin = directory.get_plugin()
        with self.network() as net:
            net_id = net['network']['id']
        port_id = 'fake_id'
        port_db = models_v2.Port(
            id=port_id, tenant_id='tenant', network_id=net_id,
            mac_address='08:00:01:02:03:04', admin_state_up=True,
            status='ACTIVE', device_id='vm_id',
            device_owner=DEVICE_OWNER_COMPUTE
        )
        with self.context.session.begin():
            self.context.session.add(port_db)
        self.assertIsNone(plugin._port_provisioned('port', 'evt', 'trigger',
                                                   self.context, port_id))

    def test__port_provisioned_port_admin_state_down(self):
        plugin = directory.get_plugin()
        ups = mock.patch.object(plugin, 'update_port_status').start()
        port_id = 'fake_port_id'
        binding = mock.Mock(vif_type=portbindings.VIF_TYPE_OVS)
        port = mock.Mock(
            id=port_id, admin_state_up=False, port_binding=binding)
        with mock.patch('neutron.plugins.ml2.plugin.db.get_port',
                        return_value=port):
            plugin._port_provisioned('port', 'evt', 'trigger',
                                     self.context, port_id)
        self.assertFalse(ups.called)

    def test_create_router_port_and_fail_create_postcommit(self):

        with mock.patch.object(managers.MechanismManager,
                               'create_port_postcommit',
                               side_effect=ml2_exc.MechanismDriverError(
                                   method='create_port_postcommit')):
            l3_plugin = directory.get_plugin(constants.L3)
            data = {'router': {'name': 'router', 'admin_state_up': True,
                               'tenant_id': self.context.tenant_id}}
            r = l3_plugin.create_router(self.context, data)
            with self.subnet() as s:
                data = {'subnet_id': s['subnet']['id']}
                self.assertRaises(ml2_exc.MechanismDriverError,
                                  l3_plugin.add_router_interface,
                                  self.context, r['id'], data)
                res_ports = self._list('ports')['ports']
                self.assertEqual([], res_ports)

    def test_create_router_port_and_fail_bind_port_if_needed(self):

        with mock.patch.object(ml2_plugin.Ml2Plugin, '_bind_port_if_needed',
                               side_effect=ml2_exc.MechanismDriverError(
                                   method='_bind_port_if_needed')):
            l3_plugin = directory.get_plugin(constants.L3)
            data = {'router': {'name': 'router', 'admin_state_up': True,
                               'tenant_id': self.context.tenant_id}}
            r = l3_plugin.create_router(self.context, data)
            with self.subnet() as s:
                data = {'subnet_id': s['subnet']['id']}
                self.assertRaises(ml2_exc.MechanismDriverError,
                                  l3_plugin.add_router_interface,
                                  self.context, r['id'], data)
                res_ports = self._list('ports')['ports']
                self.assertEqual([], res_ports)

    def test_update_port_status_build(self):
        with self.port() as port:
            self.assertEqual('DOWN', port['port']['status'])
            self.assertEqual('DOWN', self.port_create_status)

    def test_notify_port_updated_for_status_change(self):
        ctx = context.get_admin_context()
        plugin = directory.get_plugin()
        with self.port() as port:
            with mock.patch.object(self.plugin,
                                   '_notify_port_updated') as notify_mock:
                port['port']['status'] = constants.PORT_STATUS_ACTIVE
                plugin.update_port(ctx, port['port']['id'], port)
                self.assertTrue(notify_mock.called)

    def test_update_port_status_short_id(self):
        ctx = context.get_admin_context()
        plugin = directory.get_plugin()
        with self.port() as port:
            with mock.patch.object(ml2_db, 'get_binding_levels',
                                   return_value=[]) as mock_gbl:
                port_id = port['port']['id']
                short_id = port_id[:11]
                plugin.update_port_status(ctx, short_id, 'UP')
                mock_gbl.assert_called_once_with(mock.ANY, port_id, mock.ANY)

    def test_update_port_with_empty_data(self):
        ctx = context.get_admin_context()
        plugin = directory.get_plugin()
        with self.port() as port:
            port_id = port['port']['id']
            new_port = plugin.update_port(ctx, port_id, {"port": {}})
            self.assertEqual(port["port"], new_port)

    def _add_fake_dhcp_agent(self):
        agent = mock.Mock(configurations='{"notifies_port_ready": true}')
        plugin = directory.get_plugin()
        self.get_dhcp_mock = mock.patch.object(
            plugin, 'get_dhcp_agents_hosting_networks',
            return_value=[agent]).start()

    def test_dhcp_provisioning_blocks_inserted_on_create_with_agents(self):
        self._add_fake_dhcp_agent()
        with mock.patch.object(provisioning_blocks,
                               'add_provisioning_component') as ap:
            with self.port():
                self.assertTrue(ap.called)

    def test_dhcp_provisioning_blocks_skipped_with_network_port(self):
        self._add_fake_dhcp_agent()
        with mock.patch.object(provisioning_blocks,
                               'add_provisioning_component') as ap:
            with self.port(device_owner=constants.DEVICE_OWNER_DHCP):
                self.assertFalse(ap.called)

    def test_dhcp_provisioning_blocks_skipped_on_create_with_no_dhcp(self):
        self._add_fake_dhcp_agent()
        with self.subnet(enable_dhcp=False) as subnet:
            with mock.patch.object(provisioning_blocks,
                                   'add_provisioning_component') as ap:
                with self.port(subnet=subnet):
                    self.assertFalse(ap.called)

    def _test_dhcp_provisioning_blocks_inserted_on_update(self, update_dict,
                                                          expected_block):
        ctx = context.get_admin_context()
        plugin = directory.get_plugin()
        self._add_fake_dhcp_agent()
        with self.port() as port:
            with mock.patch.object(provisioning_blocks,
                                   'add_provisioning_component') as ap:
                port['port'].update(update_dict)
                plugin.update_port(ctx, port['port']['id'], port)
                self.assertEqual(expected_block, ap.called)

    def test_dhcp_provisioning_blocks_not_inserted_on_no_addr_change(self):
        update = {'binding:host_id': 'newhost'}
        self._test_dhcp_provisioning_blocks_inserted_on_update(update, False)

    def test_dhcp_provisioning_blocks_inserted_on_addr_change(self):
        update = {'binding:host_id': 'newhost',
                  'mac_address': '11:22:33:44:55:66'}
        self._test_dhcp_provisioning_blocks_inserted_on_update(update, True)

    def test_dhcp_provisioning_blocks_removed_without_dhcp_agents(self):
        with mock.patch.object(provisioning_blocks,
                               'remove_provisioning_component') as cp:
            with self.port():
                self.assertTrue(cp.called)

    def test_create_update_get_port_same_fixed_ips_order(self):
        ctx = context.get_admin_context()
        plugin = directory.get_plugin()
        initial_fixed_ips = [{'ip_address': '10.0.0.5'},
                             {'ip_address': '10.0.0.7'},
                             {'ip_address': '10.0.0.6'}]
        with self.port(fixed_ips=initial_fixed_ips) as port:
            show = plugin.get_port(ctx, port['port']['id'])
            self.assertEqual(port['port']['fixed_ips'], show['fixed_ips'])
            new_fixed_ips = list(reversed(initial_fixed_ips))
            port['port']['fixed_ips'] = new_fixed_ips
            updated = plugin.update_port(ctx, port['port']['id'], port)
            self.assertEqual(show['fixed_ips'], updated['fixed_ips'])
            updated = plugin.get_port(ctx, port['port']['id'])
            self.assertEqual(show['fixed_ips'], updated['fixed_ips'])

    def test_update_port_fixed_ip_changed(self):
        ctx = context.get_admin_context()
        plugin = directory.get_plugin()
        fixed_ip_data = [{'ip_address': '10.0.0.4'}]
        with self.port(fixed_ips=fixed_ip_data) as port,\
            mock.patch.object(
                plugin.notifier,
                'security_groups_member_updated') as sg_member_update:
            port['port']['fixed_ips'][0]['ip_address'] = '10.0.0.3'
            plugin.update_port(ctx, port['port']['id'], port)
            self.assertTrue(sg_member_update.called)

    def test_update_port_status_with_network(self):
        registry.clear()  # don't care about callback behavior
        ctx = context.get_admin_context()
        plugin = directory.get_plugin()
        with self.port() as port:
            net = plugin.get_network(ctx, port['port']['network_id'])
            with mock.patch.object(plugin, 'get_networks') as get_nets:
                plugin.update_port_status(ctx, port['port']['id'], 'UP',
                                          network=net)
                self.assertFalse(get_nets.called)

    def test_update_port_mac(self):
        self.check_update_port_mac(
            host_arg={portbindings.HOST_ID: HOST},
            arg_list=(portbindings.HOST_ID,))

    def test_update_non_existent_port(self):
        ctx = context.get_admin_context()
        plugin = directory.get_plugin()
        data = {'port': {'admin_state_up': False}}
        self.assertRaises(exc.PortNotFound, plugin.update_port, ctx,
                          'invalid-uuid', data)

    def test_delete_non_existent_port(self):
        ctx = context.get_admin_context()
        plugin = directory.get_plugin()
        with mock.patch.object(ml2_plugin.LOG, 'debug') as log_debug:
            plugin.delete_port(ctx, 'invalid-uuid', l3_port_check=False)
            log_debug.assert_has_calls([
                mock.call(_("Deleting port %s"), 'invalid-uuid'),
                mock.call(_("The port '%s' was deleted"), 'invalid-uuid')
            ])

    def test_l3_cleanup_on_net_delete(self):
        l3plugin = directory.get_plugin(constants.L3)
        kwargs = {'arg_list': (external_net.EXTERNAL,),
                  external_net.EXTERNAL: True}
        with self.network(**kwargs) as n:
            with self.subnet(network=n, cidr='200.0.0.0/22'):
                l3plugin.create_floatingip(
                    context.get_admin_context(),
                    {'floatingip': {'floating_network_id': n['network']['id'],
                                    'tenant_id': n['network']['tenant_id'],
                                    'dns_name': '', 'dns_domain': ''}}
                )
        self._delete('networks', n['network']['id'])
        flips = l3plugin.get_floatingips(context.get_admin_context())
        self.assertFalse(flips)

    def test_create_ports_bulk_port_binding_failure(self):
        ctx = context.get_admin_context()
        with self.network() as net:
            plugin = directory.get_plugin()

            with mock.patch.object(plugin, '_bind_port_if_needed',
                side_effect=ml2_exc.MechanismDriverError(
                    method='create_port_bulk')) as _bind_port_if_needed:

                res = self._create_port_bulk(self.fmt, 2, net['network']['id'],
                                             'test', True, context=ctx)

                self.assertTrue(_bind_port_if_needed.called)
                # We expect a 500 as we injected a fault in the plugin
                self._validate_behavior_on_bulk_failure(
                    res, 'ports', webob.exc.HTTPServerError.code)

    def test_create_ports_bulk_with_sec_grp(self):
        ctx = context.get_admin_context()
        plugin = directory.get_plugin()
        with self.network() as net,\
                mock.patch.object(plugin.notifier,
                                  'security_groups_member_updated') as m_upd,\
                mock.patch.object(plugin.notifier,
                                  'security_groups_provider_updated') as p_upd:

            res = self._create_port_bulk(self.fmt, 3, net['network']['id'],
                                         'test', True, context=ctx)
            ports = self.deserialize(self.fmt, res)
            used_sg = ports['ports'][0]['security_groups']
            m_upd.assert_called_once_with(ctx, used_sg)
            self.assertFalse(p_upd.called)

    def _check_security_groups_provider_updated_args(self, p_upd_mock, net_id):
        query_params = "network_id=%s" % net_id
        network_ports = self._list('ports', query_params=query_params)
        network_ports_ids = [port['id'] for port in network_ports['ports']]
        self.assertTrue(p_upd_mock.called)
        p_upd_args = p_upd_mock.call_args
        ports_ids = p_upd_args[0][1]
        self.assertEqual(sorted(network_ports_ids), sorted(ports_ids))

    def test_create_ports_bulk_with_sec_grp_member_provider_update(self):
        ctx = context.get_admin_context()
        plugin = directory.get_plugin()
        with self.network() as net,\
                mock.patch.object(plugin.notifier,
                                  'security_groups_member_updated') as m_upd,\
                mock.patch.object(plugin.notifier,
                                  'security_groups_provider_updated') as p_upd:

            net_id = net['network']['id']
            data = [{
                    'network_id': net_id,
                    'tenant_id': self._tenant_id
                    },
                    {
                    'network_id': net_id,
                    'tenant_id': self._tenant_id,
                    'device_owner': constants.DEVICE_OWNER_DHCP
                    }
                    ]

            res = self._create_bulk_from_list(self.fmt, 'port',
                                              data, context=ctx)
            ports = self.deserialize(self.fmt, res)
            used_sg = ports['ports'][0]['security_groups']
            m_upd.assert_called_once_with(ctx, used_sg)
            self._check_security_groups_provider_updated_args(p_upd, net_id)
            m_upd.reset_mock()
            p_upd.reset_mock()
            data[0]['device_owner'] = constants.DEVICE_OWNER_DHCP
            self._create_bulk_from_list(self.fmt, 'port',
                                        data, context=ctx)
            self.assertFalse(m_upd.called)
            self._check_security_groups_provider_updated_args(p_upd, net_id)

    def test_create_ports_bulk_with_sec_grp_provider_update_ipv6(self):
        ctx = context.get_admin_context()
        plugin = directory.get_plugin()
        fake_prefix = '2001:db8::/64'
        fake_gateway = 'fe80::1'
        with self.network() as net:
            with self.subnet(net,
                             gateway_ip=fake_gateway,
                             cidr=fake_prefix,
                             ip_version=6) as snet_v6,\
                    mock.patch.object(
                        plugin.notifier,
                        'security_groups_member_updated') as m_upd,\
                    mock.patch.object(
                        plugin.notifier,
                        'security_groups_provider_updated') as p_upd:

                net_id = net['network']['id']
                data = [{
                        'network_id': net_id,
                        'tenant_id': self._tenant_id,
                        'fixed_ips': [{'subnet_id': snet_v6['subnet']['id']}],
                        'device_owner': constants.DEVICE_OWNER_ROUTER_INTF
                        }
                        ]
                self._create_bulk_from_list(self.fmt, 'port',
                                            data, context=ctx)
                self.assertFalse(m_upd.called)
                self._check_security_groups_provider_updated_args(
                    p_upd, net_id)

    def test_delete_port_no_notify_in_disassociate_floatingips(self):
        ctx = context.get_admin_context()
        plugin = directory.get_plugin()
        l3plugin = directory.get_plugin(constants.L3)
        with self.port() as port,\
                mock.patch.object(
                    l3plugin,
                    'disassociate_floatingips') as disassociate_floatingips,\
                mock.patch.object(registry, 'notify') as notify:

            port_id = port['port']['id']
            plugin.delete_port(ctx, port_id)

            # check that no notification was requested while under
            # transaction
            disassociate_floatingips.assert_has_calls([
                mock.call(ctx, port_id, do_notify=False)
            ])

            # check that notifier was still triggered
            self.assertTrue(notify.call_counts)

    def test_registry_notify_after_port_binding(self):
        plugin = directory.get_plugin()
        ctx = context.get_admin_context()
        update_events = []
        receiver = lambda *a, **k: update_events.append(k['port'])
        registry.subscribe(receiver, resources.PORT,
                           events.AFTER_UPDATE)
        with self.port() as p:
            port = {'port': {'binding:host_id': 'newhost'}}
            plugin.update_port(ctx, p['port']['id'], port)
        # updating in the host should result in two AFTER_UPDATE events.
        # one to change the host_id, the second to commit a binding
        self.assertEqual('newhost', update_events[0]['binding:host_id'])
        self.assertEqual('unbound', update_events[0]['binding:vif_type'])
        self.assertEqual('newhost', update_events[1]['binding:host_id'])
        self.assertNotEqual('unbound', update_events[1]['binding:vif_type'])

    def test_check_if_compute_port_serviced_by_dvr(self):
        self.assertTrue(utils.is_dvr_serviced(DEVICE_OWNER_COMPUTE))

    def test_check_if_lbaas_vip_port_serviced_by_dvr(self):
        self.assertTrue(utils.is_dvr_serviced(
            constants.DEVICE_OWNER_LOADBALANCER))

    def test_check_if_lbaasv2_vip_port_serviced_by_dvr(self):
        self.assertTrue(utils.is_dvr_serviced(
            constants.DEVICE_OWNER_LOADBALANCERV2))

    def test_check_if_dhcp_port_serviced_by_dvr(self):
        self.assertTrue(utils.is_dvr_serviced(constants.DEVICE_OWNER_DHCP))

    def test_check_if_port_not_serviced_by_dvr(self):
        self.assertFalse(utils.is_dvr_serviced(
            constants.DEVICE_OWNER_ROUTER_INTF))

    def test_disassociate_floatingips_do_notify_returns_nothing(self):
        ctx = context.get_admin_context()
        l3plugin = directory.get_plugin(constants.L3)
        with self.port() as port:

            port_id = port['port']['id']
            # check that nothing is returned when notifications are handled
            # by the called method
            self.assertIsNone(l3plugin.disassociate_floatingips(ctx, port_id))

    def test_create_port_tolerates_db_deadlock(self):
        with self.network() as net:
            with self.subnet(network=net) as subnet:
                _orig = ml2_db.get_locked_port_and_binding
                self._failed = False

                def fail_once(*args, **kwargs):
                    if not self._failed:
                        self._failed = True
                        raise db_exc.DBDeadlock()
                    return _orig(*args, **kwargs)
                with mock.patch('neutron.plugins.ml2.plugin.'
                                'db.get_locked_port_and_binding',
                                side_effect=fail_once) as get_port_mock:
                    port_kwargs = {portbindings.HOST_ID: 'host1',
                                   'subnet': subnet,
                                   'device_id': 'deadlocktest'}
                    with self.port(arg_list=(portbindings.HOST_ID,),
                                   **port_kwargs) as port:
                        self.assertTrue(port['port']['id'])
                        self.assertTrue(get_port_mock.called)
                        # make sure that we didn't create more than one port on
                        # the retry
                        query_params = "network_id=%s" % net['network']['id']
                        query_params += "&device_id=%s" % 'deadlocktest'
                        ports = self._list('ports', query_params=query_params)
                        self.assertEqual(1, len(ports['ports']))

    def test_delete_port_tolerates_db_deadlock(self):
        ctx = context.get_admin_context()
        plugin = directory.get_plugin()
        with self.port() as port:
            port_db, binding = ml2_db.get_locked_port_and_binding(
                ctx, port['port']['id'])
            with mock.patch('neutron.plugins.ml2.plugin.'
                            'db.get_locked_port_and_binding') as lock:
                lock.side_effect = [db_exc.DBDeadlock,
                                    (port_db, binding)]
                req = self.new_delete_request('ports', port['port']['id'])
                res = req.get_response(self.api)
                self.assertEqual(204, res.status_int)
                self.assertEqual(2, lock.call_count)
                self.assertRaises(
                    exc.PortNotFound, plugin.get_port, ctx, port['port']['id'])

    def test_port_create_resillient_to_duplicate_records(self):

        def make_port():
            with self.port():
                pass

        self._test_operation_resillient_to_ipallocation_failure(make_port)

    def test_port_update_resillient_to_duplicate_records(self):
        cidr = '10.0.0.0/24'
        allocation_pools = [{'start': '10.0.0.2', 'end': '10.0.0.8'}]
        with self.subnet(cidr=cidr,
                         allocation_pools=allocation_pools) as subnet:
            with self.port(subnet=subnet) as p:
                data = {'port': {'fixed_ips': [{'ip_address': '10.0.0.9'}]}}
                req = self.new_update_request('ports', data, p['port']['id'])

                def do_request():
                    self.assertEqual(200,
                                     req.get_response(self.api).status_int)

                self._test_operation_resillient_to_ipallocation_failure(
                    do_request)

    def _test_operation_resillient_to_ipallocation_failure(self, func):

        class IPAllocationsGrenade(object):
            insert_ip_called = False
            except_raised = False

            def execute(self, con, curs, stmt, *args, **kwargs):
                if 'INSERT INTO ipallocations' in stmt:
                    self.insert_ip_called = True

            def commit(self, con):
                # we blow up on commit to simulate another thread/server
                # stealing our IP before our transaction was done
                if self.insert_ip_called and not self.except_raised:
                    self.except_raised = True
                    raise db_exc.DBDuplicateEntry()

        listener = IPAllocationsGrenade()
        engine = db_api.context_manager.writer.get_engine()
        db_api.sqla_listen(engine, 'before_cursor_execute', listener.execute)
        db_api.sqla_listen(engine, 'commit', listener.commit)
        func()
        # make sure that the grenade went off during the commit
        self.assertTrue(listener.except_raised)


class TestMl2PortsV2WithRevisionPlugin(Ml2PluginV2TestCase):

    def setUp(self):
        super(TestMl2PortsV2WithRevisionPlugin, self).setUp()
        self.revision_plugin = revision_plugin.RevisionPlugin()

    def test_update_port_status_bumps_revision(self):
        ctx = context.get_admin_context()
        plugin = directory.get_plugin()
        host_arg = {portbindings.HOST_ID: HOST}
        with self.port(arg_list=(portbindings.HOST_ID,),
                       **host_arg) as port:
            port = plugin.get_port(ctx, port['port']['id'])
            updated_ports = []
            receiver = lambda *a, **k: updated_ports.append(k['port'])
            registry.subscribe(receiver, resources.PORT,
                               events.AFTER_UPDATE)
            plugin.update_port_status(
                ctx, port['id'],
                constants.PORT_STATUS_ACTIVE, host=HOST)
            self.assertGreater(updated_ports[0]['revision_number'],
                               port['revision_number'])

    def test_update_port_status_dvr_port_no_update_on_same_status(self):
        ctx = context.get_admin_context()
        plugin = directory.get_plugin()
        # enable subscription for events
        p_update_receiver = mock.Mock()
        registry.subscribe(p_update_receiver, resources.PORT,
                           events.AFTER_UPDATE)
        host_arg = {portbindings.HOST_ID: HOST}
        with self.port(device_owner=constants.DEVICE_OWNER_DVR_INTERFACE,
                       device_id=TEST_ROUTER_ID,
                       arg_list=(portbindings.HOST_ID,),
                       **host_arg) as port:
            ml2_db.ensure_distributed_port_binding(ctx, port['port']['id'],
                                                   HOST)
            p_update_receiver.reset_mock()
            plugin.update_port_status(
                ctx, port['port']['id'],
                constants.PORT_STATUS_ACTIVE, host=HOST)
            self.assertTrue(p_update_receiver.called)
            after_1 = plugin.get_port(ctx, port['port']['id'])
            p_update_receiver.reset_mock()
            plugin.update_port_status(
                ctx, port['port']['id'],
                constants.PORT_STATUS_ACTIVE, host=HOST)
            self.assertFalse(p_update_receiver.called)
            after_2 = plugin.get_port(ctx, port['port']['id'])
            self.assertEqual(after_1['revision_number'],
                             after_2['revision_number'])


class TestMl2PortsV2WithL3(test_plugin.TestPortsV2, Ml2PluginV2TestCase):
    """For testing methods that require the L3 service plugin."""

    def test_update_port_status_notify_port_event_after_update(self):
        ctx = context.get_admin_context()
        plugin = directory.get_plugin()
        # enable subscription for events
        l3_router_plugin.L3RouterPlugin()
        l3plugin = directory.get_plugin(constants.L3)
        host_arg = {portbindings.HOST_ID: HOST}
        with mock.patch.object(l3plugin.l3_rpc_notifier,
                               'routers_updated_on_host') as mock_updated:
            with self.port(device_owner=constants.DEVICE_OWNER_ROUTER_HA_INTF,
                           device_id=TEST_ROUTER_ID,
                           arg_list=(portbindings.HOST_ID,),
                           **host_arg) as port:
                plugin.update_port_status(
                    ctx, port['port']['id'],
                    constants.PORT_STATUS_ACTIVE, host=HOST)
                mock_updated.assert_called_once_with(
                    mock.ANY, [TEST_ROUTER_ID], HOST)


class TestMl2PluginOnly(Ml2PluginV2TestCase):
    """For testing methods that don't call drivers"""

    def test__verify_service_plugins_requirements(self):
        plugin = directory.get_plugin()
        with mock.patch.dict(ml2_plugin.SERVICE_PLUGINS_REQUIRED_DRIVERS,
                             {self.l3_plugin: self._mechanism_drivers}),\
                mock.patch.object(plugin.extension_manager,
                                  'names',
                                  return_value=self._mechanism_drivers):

            plugin._verify_service_plugins_requirements()

    def test__verify_service_plugins_requirements_missing_driver(self):
        plugin = directory.get_plugin()
        with mock.patch.dict(ml2_plugin.SERVICE_PLUGINS_REQUIRED_DRIVERS,
                             {self.l3_plugin: ['test_required_driver']}),\
                mock.patch.object(plugin.extension_manager,
                                  'names',
                                  return_value=self._mechanism_drivers):

            self.assertRaises(
                ml2_exc.ExtensionDriverNotFound,
                plugin._verify_service_plugins_requirements
            )

    def _test_check_mac_update_allowed(self, vif_type, expect_change=True):
        plugin = directory.get_plugin()
        port = {'mac_address': "fake_mac", 'id': "fake_id"}
        if expect_change:
            new_attrs = {"mac_address": "dummy_mac"}
        else:
            new_attrs = {"mac_address": port['mac_address']}
        binding = mock.Mock()
        binding.vif_type = vif_type
        mac_changed = plugin._check_mac_update_allowed(port, new_attrs,
                                                       binding)
        self.assertEqual(expect_change, mac_changed)

    def test_check_mac_update_allowed_if_no_mac_change(self):
        self._test_check_mac_update_allowed(portbindings.VIF_TYPE_UNBOUND,
                                            expect_change=False)

    def test_check_mac_update_allowed_unless_bound(self):
        with testtools.ExpectedException(exc.PortBound):
            self._test_check_mac_update_allowed(portbindings.VIF_TYPE_OVS)

    def _test_reset_mac_for_direct_physical(self, direct_physical=True,
                                            unbinding=True):
        plugin = directory.get_plugin()
        port = {'device_id': '123', 'device_owner': 'compute:nova'}
        new_attrs = ({'device_id': '', 'device_owner': ''} if unbinding else
            {'name': 'new'})
        binding = mock.Mock()
        binding.vnic_type = (
            portbindings.VNIC_DIRECT_PHYSICAL if direct_physical else
            portbindings.VNIC_NORMAL)
        new_mac = plugin._reset_mac_for_direct_physical(
            port, new_attrs, binding)
        if direct_physical and unbinding:
            self.assertTrue(new_mac)
            self.assertIsNotNone(new_attrs.get('mac_address'))
        else:
            self.assertFalse(new_mac)
            self.assertIsNone(new_attrs.get('mac_address'))

    def test_reset_mac_for_direct_physical(self):
        self._test_reset_mac_for_direct_physical()

    def test_reset_mac_for_direct_physical_not_physycal(self):
        self._test_reset_mac_for_direct_physical(False, True)

    def test_reset_mac_for_direct_physical_no_unbinding(self):
        self._test_reset_mac_for_direct_physical(True, False)

    def test_reset_mac_for_direct_physical_no_unbinding_not_physical(self):
        self._test_reset_mac_for_direct_physical(False, False)

    def test__device_to_port_id_prefix_names(self):
        input_output = [('sg-abcdefg', 'abcdefg'),
                        ('tap123456', '123456'),
                        ('qvo567890', '567890')]
        for device, expected in input_output:
            self.assertEqual(expected,
                             ml2_plugin.Ml2Plugin._device_to_port_id(
                                 self.context, device))

    def test__device_to_port_id_mac_address(self):
        with self.port() as p:
            mac = p['port']['mac_address']
            port_id = p['port']['id']
            self.assertEqual(port_id,
                             ml2_plugin.Ml2Plugin._device_to_port_id(
                                 self.context, mac))

    def test__device_to_port_id_not_uuid_not_mac(self):
        dev = '1234567'
        self.assertEqual(dev, ml2_plugin.Ml2Plugin._device_to_port_id(
            self.context, dev))

    def test__device_to_port_id_UUID(self):
        port_id = uuidutils.generate_uuid()
        self.assertEqual(port_id, ml2_plugin.Ml2Plugin._device_to_port_id(
            self.context, port_id))


class Test_GetNetworkMtu(Ml2PluginV2TestCase):

    def test_get_mtu_with_physical_net(self):
        plugin = directory.get_plugin()
        mock_type_driver = mock.MagicMock()
        plugin.type_manager.drivers['driver1'] = mock.Mock()
        plugin.type_manager.drivers['driver1'].obj = mock_type_driver
        net = {
            'name': 'net1',
            pnet.NETWORK_TYPE: 'driver1',
            pnet.PHYSICAL_NETWORK: 'physnet1',
        }
        plugin._get_network_mtu(net)
        mock_type_driver.get_mtu.assert_called_once_with('physnet1')

    def _register_type_driver_with_mtu(self, driver, mtu):
        plugin = directory.get_plugin()

        class FakeDriver(object):
            def get_mtu(self, physical_network=None):
                return mtu

        driver_mock = mock.Mock()
        driver_mock.obj = FakeDriver()
        plugin.type_manager.drivers[driver] = driver_mock

    def test_single_segment(self):
        plugin = directory.get_plugin()
        self._register_type_driver_with_mtu('driver1', 1400)

        net = {
            'name': 'net1',
            mpnet.SEGMENTS: [
                {
                    pnet.NETWORK_TYPE: 'driver1',
                    pnet.PHYSICAL_NETWORK: 'physnet1'
                },
            ]
        }
        self.assertEqual(1400, plugin._get_network_mtu(net))

    def test_multiple_segments_returns_minimal_mtu(self):
        plugin = directory.get_plugin()
        self._register_type_driver_with_mtu('driver1', 1400)
        self._register_type_driver_with_mtu('driver2', 1300)

        net = {
            'name': 'net1',
            mpnet.SEGMENTS: [
                {
                    pnet.NETWORK_TYPE: 'driver1',
                    pnet.PHYSICAL_NETWORK: 'physnet1'
                },
                {
                    pnet.NETWORK_TYPE: 'driver2',
                    pnet.PHYSICAL_NETWORK: 'physnet2'
                },
            ]
        }
        self.assertEqual(1300, plugin._get_network_mtu(net))

    def test_no_segments(self):
        plugin = directory.get_plugin()
        self._register_type_driver_with_mtu('driver1', 1400)

        net = {
            'name': 'net1',
            pnet.NETWORK_TYPE: 'driver1',
            pnet.PHYSICAL_NETWORK: 'physnet1',
        }
        self.assertEqual(1400, plugin._get_network_mtu(net))

    def test_get_mtu_None_returns_0(self):
        plugin = directory.get_plugin()
        self._register_type_driver_with_mtu('driver1', None)

        net = {
            'name': 'net1',
            pnet.NETWORK_TYPE: 'driver1',
            pnet.PHYSICAL_NETWORK: 'physnet1',
        }
        self.assertEqual(0, plugin._get_network_mtu(net))

    def test_unknown_segment_type_ignored(self):
        plugin = directory.get_plugin()
        self._register_type_driver_with_mtu('driver1', None)
        self._register_type_driver_with_mtu('driver2', 1300)

        net = {
            'name': 'net1',
            mpnet.SEGMENTS: [
                {
                    pnet.NETWORK_TYPE: 'driver1',
                    pnet.PHYSICAL_NETWORK: 'physnet1'
                },
                {
                    pnet.NETWORK_TYPE: 'driver2',
                    pnet.PHYSICAL_NETWORK: 'physnet2'
                },
            ]
        }
        self.assertEqual(1300, plugin._get_network_mtu(net))


class TestMl2DvrPortsV2(TestMl2PortsV2):
    def setUp(self):
        super(TestMl2DvrPortsV2, self).setUp()
        extensions = ['router',
                      constants.L3_AGENT_SCHEDULER_EXT_ALIAS,
                      constants.L3_DISTRIBUTED_EXT_ALIAS]
        self.plugin = directory.get_plugin()
        self.l3plugin = mock.Mock()
        type(self.l3plugin).supported_extension_aliases = (
            mock.PropertyMock(return_value=extensions))

    def test_delete_port_notifies_l3_plugin(self, floating_ip=False):
        directory.add_plugin(constants.L3, self.l3plugin)
        ns_to_delete = {'host': 'myhost', 'agent_id': 'vm_l3_agent',
                        'router_id': 'my_router'}
        router_ids = set()
        if floating_ip:
            router_ids.add(ns_to_delete['router_id'])

        with self.port() as port,\
                mock.patch.object(registry, 'notify') as notify,\
                mock.patch.object(self.l3plugin,
                                  'disassociate_floatingips',
                                  return_value=router_ids):
            port_id = port['port']['id']
            self.plugin.delete_port(self.context, port_id)
            self.assertEqual(2, notify.call_count)
            # needed for a full match in the assertion below
            port['port']['extra_dhcp_opts'] = []
            expected = [mock.call(resources.PORT, events.BEFORE_DELETE,
                                  mock.ANY, context=self.context,
                                  port_id=port['port']['id'], port_check=True),
                        mock.call(resources.PORT, events.AFTER_DELETE,
                                  mock.ANY, context=self.context,
                                  port=port['port'],
                                  router_ids=router_ids)]
            notify.assert_has_calls(expected)

    def test_delete_port_with_floatingip_notifies_l3_plugin(self):
        self.test_delete_port_notifies_l3_plugin(floating_ip=True)

    def test_concurrent_csnat_port_delete(self):
        plugin = directory.get_plugin(constants.L3)
        r = plugin.create_router(
            self.context,
            {'router': {'name': 'router', 'admin_state_up': True,
             'tenant_id': self.context.tenant_id}})
        with self.subnet() as s:
            p = plugin.add_router_interface(self.context, r['id'],
                                            {'subnet_id': s['subnet']['id']})

        # lie to turn the port into an SNAT interface
        with self.context.session.begin():
            rp = self.context.session.query(l3_models.RouterPort).filter_by(
                port_id=p['port_id']).first()
            rp.port_type = constants.DEVICE_OWNER_ROUTER_SNAT

        # take the port away before csnat gets a chance to delete it
        # to simulate a concurrent delete
        orig_get_ports = plugin._core_plugin.get_ports

        def get_ports_with_delete_first(*args, **kwargs):
            plugin._core_plugin.delete_port(self.context,
                                            p['port_id'],
                                            l3_port_check=False)
            return orig_get_ports(*args, **kwargs)
        plugin._core_plugin.get_ports = get_ports_with_delete_first

        # This should be able to handle a concurrent delete without raising
        # an exception
        router = plugin._get_router(self.context, r['id'])
        plugin.delete_csnat_router_interface_ports(self.context, router)


class TestMl2PortBinding(Ml2PluginV2TestCase,
                         test_bindings.PortBindingsTestCase):
    # Test case does not set binding:host_id, so ml2 does not attempt
    # to bind port
    VIF_TYPE = portbindings.VIF_TYPE_UNBOUND
    HAS_PORT_FILTER = False
    ENABLE_SG = True
    FIREWALL_DRIVER = test_sg_rpc.FIREWALL_HYBRID_DRIVER

    def setUp(self, firewall_driver=None):
        test_sg_rpc.set_firewall_driver(self.FIREWALL_DRIVER)
        config.cfg.CONF.set_override(
            'enable_security_group', self.ENABLE_SG,
            group='SECURITYGROUP')
        super(TestMl2PortBinding, self).setUp()

    def _check_port_binding_profile(self, port, profile=None):
        self.assertIn('id', port)
        self.assertIn(portbindings.PROFILE, port)
        value = port[portbindings.PROFILE]
        self.assertEqual(profile or {}, value)

    def test_create_port_binding_profile(self):
        self._test_create_port_binding_profile({'a': 1, 'b': 2})

    def test_update_port_binding_profile(self):
        self._test_update_port_binding_profile({'c': 3})

    def test_create_port_binding_profile_too_big(self):
        s = 'x' * 5000
        profile_arg = {portbindings.PROFILE: {'d': s}}
        try:
            with self.port(expected_res_status=400,
                           arg_list=(portbindings.PROFILE,),
                           **profile_arg):
                pass
        except webob.exc.HTTPClientError:
            pass

    def test_remove_port_binding_profile(self):
        profile = {'e': 5}
        profile_arg = {portbindings.PROFILE: profile}
        with self.port(arg_list=(portbindings.PROFILE,),
                       **profile_arg) as port:
            self._check_port_binding_profile(port['port'], profile)
            port_id = port['port']['id']
            profile_arg = {portbindings.PROFILE: None}
            port = self._update('ports', port_id,
                                {'port': profile_arg})['port']
            self._check_port_binding_profile(port)
            port = self._show('ports', port_id)['port']
            self._check_port_binding_profile(port)

    def test_return_on_concurrent_delete_and_binding(self):
        # create a port and delete it so we have an expired mechanism context
        with self.port() as port:
            plugin = directory.get_plugin()
            binding = ml2_db.get_locked_port_and_binding(self.context,
                                                         port['port']['id'])[1]
            binding['host'] = 'test'
            mech_context = driver_context.PortContext(
                plugin, self.context, port['port'],
                plugin.get_network(self.context, port['port']['network_id']),
                binding, None)
        with mock.patch(
            'neutron.plugins.ml2.plugin.' 'db.get_locked_port_and_binding',
            return_value=(None, None)) as glpab_mock,\
                mock.patch('neutron.plugins.ml2.plugin.Ml2Plugin.'
                           '_make_port_dict') as mpd_mock:
            plugin._bind_port_if_needed(mech_context)
            # called during deletion to get port
            self.assertTrue(glpab_mock.mock_calls)
            # should have returned before calling _make_port_dict
            self.assertFalse(mpd_mock.mock_calls)

    def _create_port_and_bound_context(self, port_vif_type, bound_vif_type):
        with self.port() as port:
            plugin = directory.get_plugin()
            binding = ml2_db.get_locked_port_and_binding(self.context,
                                                         port['port']['id'])[1]
            binding['host'] = 'fake_host'
            binding['vif_type'] = port_vif_type
            # Generates port context to be used before the bind.
            port_context = driver_context.PortContext(
                plugin, self.context, port['port'],
                plugin.get_network(self.context, port['port']['network_id']),
                binding, None)
            bound_context = mock.MagicMock()
            # Bound context is how port_context is expected to look
            # after _bind_port.
            bound_context.vif_type = bound_vif_type
            return plugin, port_context, bound_context

    def test__attempt_binding(self):
        # Simulate a successful binding for vif_type unbound
        # and keep the same binding state for other vif types.
        vif_types = [(portbindings.VIF_TYPE_BINDING_FAILED,
                      portbindings.VIF_TYPE_BINDING_FAILED),
                     (portbindings.VIF_TYPE_UNBOUND,
                      portbindings.VIF_TYPE_OVS),
                     (portbindings.VIF_TYPE_OVS,
                      portbindings.VIF_TYPE_OVS)]

        for port_vif_type, bound_vif_type in vif_types:
            plugin, port_context, bound_context = (
                self._create_port_and_bound_context(port_vif_type,
                                                    bound_vif_type))
            with mock.patch('neutron.plugins.ml2.plugin.Ml2Plugin._bind_port',
                            return_value=bound_context) as bd_mock:
                context, need_notify, try_again = (plugin._attempt_binding(
                    port_context, False))
                expected_need_notify = port_vif_type not in (
                    portbindings.VIF_TYPE_BINDING_FAILED,
                    portbindings.VIF_TYPE_OVS)

                if bound_vif_type == portbindings.VIF_TYPE_BINDING_FAILED:
                    expected_vif_type = port_vif_type
                    expected_try_again = True
                    expected_bd_mock_called = True
                else:
                    expected_vif_type = portbindings.VIF_TYPE_OVS
                    expected_try_again = False
                    expected_bd_mock_called = (port_vif_type ==
                                               portbindings.VIF_TYPE_UNBOUND)

                self.assertEqual(expected_need_notify, need_notify)
                self.assertEqual(expected_vif_type, context.vif_type)
                self.assertEqual(expected_try_again, try_again)
                self.assertEqual(expected_bd_mock_called, bd_mock.called)

    def test__attempt_binding_retries(self):
        # Simulate cases of both successful and failed binding states for
        # vif_type unbound
        vif_types = [(portbindings.VIF_TYPE_UNBOUND,
                      portbindings.VIF_TYPE_BINDING_FAILED),
                     (portbindings.VIF_TYPE_UNBOUND,
                      portbindings.VIF_TYPE_OVS)]

        for port_vif_type, bound_vif_type in vif_types:
            plugin, port_context, bound_context = (
                self._create_port_and_bound_context(port_vif_type,
                                                    bound_vif_type))
            with mock.patch(
                    'neutron.plugins.ml2.plugin.Ml2Plugin._bind_port',
                    return_value=bound_context),\
                    mock.patch('neutron.plugins.ml2.plugin.Ml2Plugin._commit_'
                               'port_binding',
                               return_value=(bound_context, True, False)),\
                    mock.patch('neutron.plugins.ml2.plugin.Ml2Plugin.'
                               '_attempt_binding',
                               side_effect=plugin._attempt_binding) as at_mock:
                    plugin._bind_port_if_needed(port_context)
                    if bound_vif_type == portbindings.VIF_TYPE_BINDING_FAILED:
                        # An unsuccessful binding attempt should be retried
                        # MAX_BIND_TRIES amount of times.
                        self.assertEqual(ml2_plugin.MAX_BIND_TRIES,
                                         at_mock.call_count)
                    else:
                        # Successful binding should only be attempted once.
                        self.assertEqual(1, at_mock.call_count)

    def test_port_binding_profile_not_changed(self):
        profile = {'e': 5}
        profile_arg = {portbindings.PROFILE: profile}
        with self.port(arg_list=(portbindings.PROFILE,),
                       **profile_arg) as port:
            self._check_port_binding_profile(port['port'], profile)
            port_id = port['port']['id']
            state_arg = {'admin_state_up': True}
            port = self._update('ports', port_id,
                                {'port': state_arg})['port']
            self._check_port_binding_profile(port, profile)
            port = self._show('ports', port_id)['port']
            self._check_port_binding_profile(port, profile)

    def test_update_port_binding_host_id_none(self):
        with self.port() as port:
            plugin = directory.get_plugin()
            binding = ml2_db.get_locked_port_and_binding(self.context,
                                                         port['port']['id'])[1]
            binding['host'] = 'test'
            mech_context = driver_context.PortContext(
                plugin, self.context, port['port'],
                plugin.get_network(self.context, port['port']['network_id']),
                binding, None)
        with mock.patch('neutron.plugins.ml2.plugin.Ml2Plugin.'
                        '_update_port_dict_binding') as update_mock:
            attrs = {portbindings.HOST_ID: None}
            plugin._process_port_binding(mech_context, attrs)
            self.assertTrue(update_mock.mock_calls)
            self.assertEqual('', binding.host)

    def test_update_port_binding_host_id_not_changed(self):
        with self.port() as port:
            plugin = directory.get_plugin()
            binding = ml2_db.get_locked_port_and_binding(self.context,
                                                         port['port']['id'])[1]
            binding['host'] = 'test'
            mech_context = driver_context.PortContext(
                plugin, self.context, port['port'],
                plugin.get_network(self.context, port['port']['network_id']),
                binding, None)
        with mock.patch('neutron.plugins.ml2.plugin.Ml2Plugin.'
                        '_update_port_dict_binding') as update_mock:
            attrs = {portbindings.PROFILE: {'e': 5}}
            plugin._process_port_binding(mech_context, attrs)
            self.assertTrue(update_mock.mock_calls)
            self.assertEqual('test', binding.host)

    def test_process_distributed_port_binding_update_router_id(self):
        host_id = 'host'
        binding = models.DistributedPortBinding(
                            port_id='port_id',
                            host=host_id,
                            router_id='old_router_id',
                            vif_type=portbindings.VIF_TYPE_OVS,
                            vnic_type=portbindings.VNIC_NORMAL,
                            status=constants.PORT_STATUS_DOWN)
        plugin = directory.get_plugin()
        mock_network = {'id': 'net_id'}
        mock_port = {'id': 'port_id'}
        context = mock.Mock()
        new_router_id = 'new_router'
        attrs = {'device_id': new_router_id, portbindings.HOST_ID: host_id}
        with mock.patch.object(plugin, '_update_port_dict_binding'):
            with mock.patch.object(segments_db, 'get_network_segments',
                                   return_value=[]):
                mech_context = driver_context.PortContext(
                    self, context, mock_port, mock_network, binding, None)
                plugin._process_distributed_port_binding(mech_context,
                                                         context, attrs)
                self.assertEqual(new_router_id,
                                 mech_context._binding.router_id)
                self.assertEqual(host_id, mech_context._binding.host)

    def test_update_distributed_port_binding_on_concurrent_port_delete(self):
        plugin = directory.get_plugin()
        with self.port() as port:
            port = {
                'id': port['port']['id'],
                portbindings.HOST_ID: 'foo_host',
            }
            exc = db_exc.DBReferenceError('', '', '', '')
            with mock.patch.object(ml2_db, 'ensure_distributed_port_binding',
                                   side_effect=exc):
                res = plugin.update_distributed_port_binding(
                    self.context, port['id'], {'port': port})
        self.assertIsNone(res)

    def test_update_distributed_port_binding_on_non_existent_port(self):
        plugin = directory.get_plugin()
        port = {
            'id': 'foo_port_id',
            portbindings.HOST_ID: 'foo_host',
        }
        with mock.patch.object(
            ml2_db, 'ensure_distributed_port_binding') as mock_dist:
            plugin.update_distributed_port_binding(
                self.context, 'foo_port_id', {'port': port})
        self.assertFalse(mock_dist.called)

    def test__bind_port_original_port_set(self):
        plugin = directory.get_plugin()
        plugin.mechanism_manager = mock.Mock()
        mock_port = {'id': 'port_id'}
        context = mock.Mock()
        context.network.current = {'id': 'net_id'}
        context.original = mock_port
        with mock.patch.object(plugin, '_update_port_dict_binding'), \
            mock.patch.object(segments_db, 'get_network_segments',
                              return_value=[]):
            new_context = plugin._bind_port(context)
            self.assertEqual(mock_port, new_context.original)
            self.assertFalse(new_context == context)


class TestMl2PortBindingNoSG(TestMl2PortBinding):
    HAS_PORT_FILTER = False
    ENABLE_SG = False
    FIREWALL_DRIVER = test_sg_rpc.FIREWALL_NOOP_DRIVER


class TestMl2PortBindingHost(Ml2PluginV2TestCase,
                             test_bindings.PortBindingsHostTestCaseMixin):
    pass


class TestMl2PortBindingVnicType(Ml2PluginV2TestCase,
                                 test_bindings.PortBindingsVnicTestCaseMixin):
    pass


class TestMultiSegmentNetworks(Ml2PluginV2TestCase):

    def setUp(self, plugin=None):
        super(TestMultiSegmentNetworks, self).setUp()

    def test_allocate_dynamic_segment(self):
        data = {'network': {'name': 'net1',
                            'tenant_id': 'tenant_one'}}
        network_req = self.new_create_request('networks', data)
        network = self.deserialize(self.fmt,
                                   network_req.get_response(self.api))
        segment = {driver_api.NETWORK_TYPE: 'vlan',
                   driver_api.PHYSICAL_NETWORK: 'physnet1'}
        network_id = network['network']['id']
        self.driver.type_manager.allocate_dynamic_segment(
            self.context, network_id, segment)
        dynamic_segment = segments_db.get_dynamic_segment(
            self.context, network_id, 'physnet1')
        self.assertEqual('vlan', dynamic_segment[driver_api.NETWORK_TYPE])
        self.assertEqual('physnet1',
                         dynamic_segment[driver_api.PHYSICAL_NETWORK])
        self.assertGreater(dynamic_segment[driver_api.SEGMENTATION_ID], 0)
        segment2 = {driver_api.NETWORK_TYPE: 'vlan',
                    driver_api.SEGMENTATION_ID: 1234,
                    driver_api.PHYSICAL_NETWORK: 'physnet3'}
        self.driver.type_manager.allocate_dynamic_segment(
            self.context, network_id, segment2)
        dynamic_segment = segments_db.get_dynamic_segment(
            self.context, network_id, segmentation_id='1234')
        self.assertEqual('vlan', dynamic_segment[driver_api.NETWORK_TYPE])
        self.assertEqual('physnet3',
                         dynamic_segment[driver_api.PHYSICAL_NETWORK])
        self.assertEqual(dynamic_segment[driver_api.SEGMENTATION_ID], 1234)

    def test_allocate_dynamic_segment_multiple_physnets(self):
        data = {'network': {'name': 'net1',
                            'tenant_id': 'tenant_one'}}
        network_req = self.new_create_request('networks', data)
        network = self.deserialize(self.fmt,
                                   network_req.get_response(self.api))
        segment = {driver_api.NETWORK_TYPE: 'vlan',
                   driver_api.PHYSICAL_NETWORK: 'physnet1'}
        network_id = network['network']['id']
        self.driver.type_manager.allocate_dynamic_segment(
            self.context, network_id, segment)
        dynamic_segment = segments_db.get_dynamic_segment(
            self.context, network_id, 'physnet1')
        self.assertEqual('vlan', dynamic_segment[driver_api.NETWORK_TYPE])
        self.assertEqual('physnet1',
                         dynamic_segment[driver_api.PHYSICAL_NETWORK])
        dynamic_segmentation_id = dynamic_segment[driver_api.SEGMENTATION_ID]
        self.assertGreater(dynamic_segmentation_id, 0)
        dynamic_segment1 = segments_db.get_dynamic_segment(
            self.context, network_id, 'physnet1')
        dynamic_segment1_id = dynamic_segment1[driver_api.SEGMENTATION_ID]
        self.assertEqual(dynamic_segmentation_id, dynamic_segment1_id)
        segment2 = {driver_api.NETWORK_TYPE: 'vlan',
                    driver_api.PHYSICAL_NETWORK: 'physnet2'}
        self.driver.type_manager.allocate_dynamic_segment(
            self.context, network_id, segment2)
        dynamic_segment2 = segments_db.get_dynamic_segment(
            self.context, network_id, 'physnet2')
        dynamic_segmentation2_id = dynamic_segment2[driver_api.SEGMENTATION_ID]
        self.assertNotEqual(dynamic_segmentation_id, dynamic_segmentation2_id)

    def test_allocate_release_dynamic_segment(self):
        data = {'network': {'name': 'net1',
                            'tenant_id': 'tenant_one'}}
        network_req = self.new_create_request('networks', data)
        network = self.deserialize(self.fmt,
                                   network_req.get_response(self.api))
        segment = {driver_api.NETWORK_TYPE: 'vlan',
                   driver_api.PHYSICAL_NETWORK: 'physnet1'}
        network_id = network['network']['id']
        self.driver.type_manager.allocate_dynamic_segment(
            self.context, network_id, segment)
        dynamic_segment = segments_db.get_dynamic_segment(
            self.context, network_id, 'physnet1')
        self.assertEqual('vlan', dynamic_segment[driver_api.NETWORK_TYPE])
        self.assertEqual('physnet1',
                         dynamic_segment[driver_api.PHYSICAL_NETWORK])
        dynamic_segmentation_id = dynamic_segment[driver_api.SEGMENTATION_ID]
        self.assertGreater(dynamic_segmentation_id, 0)
        self.driver.type_manager.release_dynamic_segment(
            self.context, dynamic_segment[driver_api.ID])
        self.assertIsNone(segments_db.get_dynamic_segment(
            self.context, network_id, 'physnet1'))

    def test_create_network_provider(self):
        data = {'network': {'name': 'net1',
                            pnet.NETWORK_TYPE: 'vlan',
                            pnet.PHYSICAL_NETWORK: 'physnet1',
                            pnet.SEGMENTATION_ID: 1,
                            'tenant_id': 'tenant_one'}}
        network_req = self.new_create_request('networks', data)
        network = self.deserialize(self.fmt,
                                   network_req.get_response(self.api))
        self.assertEqual('vlan', network['network'][pnet.NETWORK_TYPE])
        self.assertEqual('physnet1', network['network'][pnet.PHYSICAL_NETWORK])
        self.assertEqual(1, network['network'][pnet.SEGMENTATION_ID])
        self.assertNotIn(mpnet.SEGMENTS, network['network'])

    def test_create_network_single_multiprovider(self):
        data = {'network': {'name': 'net1',
                            mpnet.SEGMENTS:
                            [{pnet.NETWORK_TYPE: 'vlan',
                              pnet.PHYSICAL_NETWORK: 'physnet1',
                              pnet.SEGMENTATION_ID: 1}],
                            'tenant_id': 'tenant_one'}}
        net_req = self.new_create_request('networks', data)
        network = self.deserialize(self.fmt, net_req.get_response(self.api))
        self.assertEqual('vlan', network['network'][pnet.NETWORK_TYPE])
        self.assertEqual('physnet1', network['network'][pnet.PHYSICAL_NETWORK])
        self.assertEqual(1, network['network'][pnet.SEGMENTATION_ID])
        self.assertNotIn(mpnet.SEGMENTS, network['network'])

        # Tests get_network()
        net_req = self.new_show_request('networks', network['network']['id'])
        network = self.deserialize(self.fmt, net_req.get_response(self.api))
        self.assertEqual('vlan', network['network'][pnet.NETWORK_TYPE])
        self.assertEqual('physnet1', network['network'][pnet.PHYSICAL_NETWORK])
        self.assertEqual(1, network['network'][pnet.SEGMENTATION_ID])
        self.assertNotIn(mpnet.SEGMENTS, network['network'])

    def test_create_network_multiprovider(self):
        data = {'network': {'name': 'net1',
                            mpnet.SEGMENTS:
                            [{pnet.NETWORK_TYPE: 'vlan',
                              pnet.PHYSICAL_NETWORK: 'physnet1',
                              pnet.SEGMENTATION_ID: 1},
                             {pnet.NETWORK_TYPE: 'vlan',
                              pnet.PHYSICAL_NETWORK: 'physnet1',
                              pnet.SEGMENTATION_ID: 2}],
                            'tenant_id': 'tenant_one'}}
        network_req = self.new_create_request('networks', data)
        network = self.deserialize(self.fmt,
                                   network_req.get_response(self.api))
        segments = network['network'][mpnet.SEGMENTS]
        for segment_index, segment in enumerate(data['network']
                                                [mpnet.SEGMENTS]):
            for field in [pnet.NETWORK_TYPE, pnet.PHYSICAL_NETWORK,
                          pnet.SEGMENTATION_ID]:
                self.assertEqual(segment.get(field),
                            segments[segment_index][field])

        # Tests get_network()
        net_req = self.new_show_request('networks', network['network']['id'])
        network = self.deserialize(self.fmt, net_req.get_response(self.api))
        segments = network['network'][mpnet.SEGMENTS]
        for segment_index, segment in enumerate(data['network']
                                                [mpnet.SEGMENTS]):
            for field in [pnet.NETWORK_TYPE, pnet.PHYSICAL_NETWORK,
                          pnet.SEGMENTATION_ID]:
                self.assertEqual(segment.get(field),
                            segments[segment_index][field])

    def test_create_network_with_provider_and_multiprovider_fail(self):
        data = {'network': {'name': 'net1',
                            mpnet.SEGMENTS:
                            [{pnet.NETWORK_TYPE: 'vlan',
                              pnet.PHYSICAL_NETWORK: 'physnet1',
                              pnet.SEGMENTATION_ID: 1}],
                            pnet.NETWORK_TYPE: 'vlan',
                            pnet.PHYSICAL_NETWORK: 'physnet1',
                            pnet.SEGMENTATION_ID: 1,
                            'tenant_id': 'tenant_one'}}

        network_req = self.new_create_request('networks', data)
        res = network_req.get_response(self.api)
        self.assertEqual(400, res.status_int)

    def test_create_network_duplicate_full_segments(self):
        data = {'network': {'name': 'net1',
                            mpnet.SEGMENTS:
                            [{pnet.NETWORK_TYPE: 'vlan',
                              pnet.PHYSICAL_NETWORK: 'physnet1',
                              pnet.SEGMENTATION_ID: 1},
                             {pnet.NETWORK_TYPE: 'vlan',
                              pnet.PHYSICAL_NETWORK: 'physnet1',
                              pnet.SEGMENTATION_ID: 1}],
                            'tenant_id': 'tenant_one'}}
        network_req = self.new_create_request('networks', data)
        res = network_req.get_response(self.api)
        self.assertEqual(400, res.status_int)

    def test_create_network_duplicate_partial_segments(self):
        data = {'network': {'name': 'net1',
                            mpnet.SEGMENTS:
                            [{pnet.NETWORK_TYPE: 'vlan',
                              pnet.PHYSICAL_NETWORK: 'physnet1'},
                             {pnet.NETWORK_TYPE: 'vlan',
                              pnet.PHYSICAL_NETWORK: 'physnet1'}],
                            'tenant_id': 'tenant_one'}}
        network_req = self.new_create_request('networks', data)
        res = network_req.get_response(self.api)
        self.assertEqual(201, res.status_int)

    def test_release_network_segments(self):
        data = {'network': {'name': 'net1',
                            'admin_state_up': True,
                            'shared': False,
                            pnet.NETWORK_TYPE: 'vlan',
                            pnet.PHYSICAL_NETWORK: 'physnet1',
                            pnet.SEGMENTATION_ID: 1,
                            'tenant_id': 'tenant_one'}}
        network_req = self.new_create_request('networks', data)
        res = network_req.get_response(self.api)
        network = self.deserialize(self.fmt, res)
        network_id = network['network']['id']
        segment = {driver_api.NETWORK_TYPE: 'vlan',
                   driver_api.PHYSICAL_NETWORK: 'physnet2'}
        self.driver.type_manager.allocate_dynamic_segment(
            self.context, network_id, segment)
        dynamic_segment = segments_db.get_dynamic_segment(
            self.context, network_id, 'physnet2')
        self.assertEqual('vlan', dynamic_segment[driver_api.NETWORK_TYPE])
        self.assertEqual('physnet2',
                         dynamic_segment[driver_api.PHYSICAL_NETWORK])
        self.assertGreater(dynamic_segment[driver_api.SEGMENTATION_ID], 0)

        with mock.patch.object(type_vlan.VlanTypeDriver,
                               'release_segment') as rs:
            segments_plugin_db.subscribe()
            req = self.new_delete_request('networks', network_id)
            res = req.get_response(self.api)
            self.assertEqual(2, rs.call_count)
        self.assertEqual([], segments_db.get_network_segments(
            self.context, network_id))
        self.assertIsNone(segments_db.get_dynamic_segment(
            self.context, network_id, 'physnet2'))

    def test_release_segment_no_type_driver(self):
        data = {'network': {'name': 'net1',
                            'admin_state_up': True,
                            'shared': False,
                            pnet.NETWORK_TYPE: 'vlan',
                            pnet.PHYSICAL_NETWORK: 'physnet1',
                            pnet.SEGMENTATION_ID: 1,
                            'tenant_id': 'tenant_one'}}
        network_req = self.new_create_request('networks', data)
        res = network_req.get_response(self.api)
        network = self.deserialize(self.fmt, res)
        network_id = network['network']['id']

        segment = {driver_api.NETWORK_TYPE: 'faketype',
                   driver_api.PHYSICAL_NETWORK: 'physnet1',
                   driver_api.ID: 1}
        with mock.patch('neutron.plugins.ml2.managers.LOG') as log:
            with mock.patch('neutron.plugins.ml2.managers.segments_db') as db:
                db.get_network_segments.return_value = (segment,)
                self.driver.type_manager.release_network_segments(
                    self.context, network_id)

                log.error.assert_called_once_with(
                    "Failed to release segment '%s' because "
                    "network type is not supported.", segment)

    def test_create_provider_fail(self):
        segment = {pnet.NETWORK_TYPE: None,
                   pnet.PHYSICAL_NETWORK: 'phys_net',
                   pnet.SEGMENTATION_ID: None}
        with testtools.ExpectedException(exc.InvalidInput):
            self.driver.type_manager._process_provider_create(segment)

    def test_create_network_plugin(self):
        data = {'network': {'name': 'net1',
                            'admin_state_up': True,
                            'shared': False,
                            pnet.NETWORK_TYPE: 'vlan',
                            pnet.PHYSICAL_NETWORK: 'physnet1',
                            pnet.SEGMENTATION_ID: 1,
                            'tenant_id': 'tenant_one'}}

        def raise_mechanism_exc(*args, **kwargs):
            raise ml2_exc.MechanismDriverError(
                method='create_network_postcommit')

        with mock.patch('neutron.plugins.ml2.managers.MechanismManager.'
                        'create_network_precommit', new=raise_mechanism_exc):
            with testtools.ExpectedException(ml2_exc.MechanismDriverError):
                self.driver.create_network(self.context, data)

    def test_extend_dictionary_no_segments(self):
        network = dict(name='net_no_segment', id='5', tenant_id='tenant_one')
        self.driver.type_manager.extend_network_dict_provider(self.context,
                                                              network)
        self.assertIsNone(network[pnet.NETWORK_TYPE])
        self.assertIsNone(network[pnet.PHYSICAL_NETWORK])
        self.assertIsNone(network[pnet.SEGMENTATION_ID])


class TestMl2AllowedAddressPairs(Ml2PluginV2TestCase,
                                 test_pair.TestAllowedAddressPairs):
    _extension_drivers = ['port_security']

    def setUp(self, plugin=None):
        config.cfg.CONF.set_override('extension_drivers',
                                     self._extension_drivers,
                                     group='ml2')
        super(test_pair.TestAllowedAddressPairs, self).setUp(
            plugin=PLUGIN_NAME)


class TestMl2PortSecurity(Ml2PluginV2TestCase):

    def setUp(self):
        config.cfg.CONF.set_override('extension_drivers',
                                     ['port_security'],
                                     group='ml2')
        config.cfg.CONF.set_override('enable_security_group',
                                     False,
                                     group='SECURITYGROUP')
        super(TestMl2PortSecurity, self).setUp()

    def test_port_update_without_security_groups(self):
        with self.port() as port:
            plugin = directory.get_plugin()
            ctx = context.get_admin_context()
            self.assertTrue(port['port']['port_security_enabled'])
            updated_port = plugin.update_port(
                ctx, port['port']['id'],
                {'port': {'port_security_enabled': False}})
            self.assertFalse(updated_port['port_security_enabled'])


class TestMl2HostsNetworkAccess(Ml2PluginV2TestCase):
    _mechanism_drivers = ['openvswitch', 'logger']

    def setUp(self):
        super(TestMl2HostsNetworkAccess, self).setUp()
        helpers.register_ovs_agent(
            host='host1', bridge_mappings={'physnet1': 'br-eth-1'})
        helpers.register_ovs_agent(
            host='host2', bridge_mappings={'physnet2': 'br-eth-2'})
        helpers.register_ovs_agent(
            host='host3', bridge_mappings={'physnet3': 'br-eth-3'})
        self.dhcp_agent1 = helpers.register_dhcp_agent(
            host='host1')
        self.dhcp_agent2 = helpers.register_dhcp_agent(
            host='host2')
        self.dhcp_agent3 = helpers.register_dhcp_agent(
            host='host3')
        self.dhcp_hosts = {'host1', 'host2', 'host3'}

    def test_filter_hosts_with_network_access(self):
        net = self.driver.create_network(
            self.context,
            {'network': {'name': 'net1',
                         pnet.NETWORK_TYPE: 'vlan',
                         pnet.PHYSICAL_NETWORK: 'physnet1',
                         pnet.SEGMENTATION_ID: 1,
                         'tenant_id': 'tenant_one',
                         'admin_state_up': True,
                         'shared': True}})
        observeds = self.driver.filter_hosts_with_network_access(
            self.context, net['id'], self.dhcp_hosts)
        self.assertEqual({self.dhcp_agent1.host}, observeds)

    def test_filter_hosts_with_network_access_multi_segments(self):
        net = self.driver.create_network(
            self.context,
            {'network': {'name': 'net1',
                         mpnet.SEGMENTS: [
                             {pnet.NETWORK_TYPE: 'vlan',
                              pnet.PHYSICAL_NETWORK: 'physnet1',
                              pnet.SEGMENTATION_ID: 1},
                             {pnet.NETWORK_TYPE: 'vlan',
                              pnet.PHYSICAL_NETWORK: 'physnet2',
                              pnet.SEGMENTATION_ID: 2}],
                         'tenant_id': 'tenant_one',
                         'admin_state_up': True,
                         'shared': True}})
        expecteds = {self.dhcp_agent1.host, self.dhcp_agent2.host}
        observeds = self.driver.filter_hosts_with_network_access(
            self.context, net['id'], self.dhcp_hosts)
        self.assertEqual(expecteds, observeds)

    def test_filter_hosts_with_network_access_not_supported(self):
        self.driver.mechanism_manager.host_filtering_supported = False
        observeds = self.driver.filter_hosts_with_network_access(
            self.context, 'fake_id', self.dhcp_hosts)
        self.assertEqual(self.dhcp_hosts, observeds)


class DHCPOptsTestCase(test_dhcpopts.TestExtraDhcpOpt):

    def setUp(self, plugin=None):
        super(DHCPOptsTestCase, self).setUp(plugin=PLUGIN_NAME)


class Ml2PluginV2FaultyDriverTestCase(test_plugin.NeutronDbPluginV2TestCase):

    def setUp(self):
        # Enable the test mechanism driver to ensure that
        # we can successfully call through to all mechanism
        # driver apis.
        config.cfg.CONF.set_override('mechanism_drivers',
                                     ['test', 'logger'],
                                     group='ml2')
        super(Ml2PluginV2FaultyDriverTestCase, self).setUp(PLUGIN_NAME)
        self.port_create_status = 'DOWN'


class TestFaultyMechansimDriver(Ml2PluginV2FaultyDriverTestCase):

    def test_create_network_faulty(self):

        err_msg = "Some errors"
        with mock.patch.object(mech_test.TestMechanismDriver,
                               'create_network_postcommit',
                               side_effect=(exc.InvalidInput(
                                                error_message=err_msg))):
            tenant_id = uuidutils.generate_uuid()
            data = {'network': {'name': 'net1',
                                'tenant_id': tenant_id}}
            req = self.new_create_request('networks', data)
            res = req.get_response(self.api)
            self.assertEqual(400, res.status_int)
            error = self.deserialize(self.fmt, res)
            self.assertEqual('InvalidInput',
                             error['NeutronError']['type'])
            # Check the client can see the root cause of error.
            self.assertIn(err_msg, error['NeutronError']['message'])
            query_params = "tenant_id=%s" % tenant_id
            nets = self._list('networks', query_params=query_params)
            self.assertFalse(nets['networks'])

    def test_delete_network_faulty(self):

        with mock.patch.object(mech_test.TestMechanismDriver,
                               'delete_network_postcommit',
                               side_effect=ml2_exc.MechanismDriverError):
            with mock.patch.object(mech_logger.LoggerMechanismDriver,
                                   'delete_network_postcommit') as dnp:

                data = {'network': {'name': 'net1',
                                    'tenant_id': 'tenant_one'}}
                network_req = self.new_create_request('networks', data)
                network_res = network_req.get_response(self.api)
                self.assertEqual(201, network_res.status_int)
                network = self.deserialize(self.fmt, network_res)
                net_id = network['network']['id']
                req = self.new_delete_request('networks', net_id)
                res = req.get_response(self.api)
                self.assertEqual(204, res.status_int)
                # Test if other mechanism driver was called
                self.assertTrue(dnp.called)
                self._show('networks', net_id,
                           expected_code=webob.exc.HTTPNotFound.code)

    def test_update_network_faulty(self):

        err_msg = "Some errors"
        with mock.patch.object(mech_test.TestMechanismDriver,
                               'update_network_postcommit',
                               side_effect=(exc.InvalidInput(
                                                error_message=err_msg))):
            with mock.patch.object(mech_logger.LoggerMechanismDriver,
                                   'update_network_postcommit') as unp:

                data = {'network': {'name': 'net1',
                                    'tenant_id': 'tenant_one'}}
                network_req = self.new_create_request('networks', data)
                network_res = network_req.get_response(self.api)
                self.assertEqual(201, network_res.status_int)
                network = self.deserialize(self.fmt, network_res)
                net_id = network['network']['id']

                new_name = 'a_brand_new_name'
                data = {'network': {'name': new_name}}
                req = self.new_update_request('networks', data, net_id)
                res = req.get_response(self.api)
                self.assertEqual(400, res.status_int)
                error = self.deserialize(self.fmt, res)
                self.assertEqual('InvalidInput',
                                 error['NeutronError']['type'])
                # Check the client can see the root cause of error.
                self.assertIn(err_msg, error['NeutronError']['message'])
                # Test if other mechanism driver was called
                self.assertTrue(unp.called)
                net = self._show('networks', net_id)
                self.assertEqual(new_name, net['network']['name'])

                self._delete('networks', net_id)

    def test_create_subnet_faulty(self):

        err_msg = "Some errors"
        with mock.patch.object(mech_test.TestMechanismDriver,
                               'create_subnet_postcommit',
                               side_effect=(exc.InvalidInput(
                                                error_message=err_msg))):

            with self.network() as network:
                net_id = network['network']['id']
                data = {'subnet': {'network_id': net_id,
                                   'cidr': '10.0.20.0/24',
                                   'ip_version': '4',
                                   'name': 'subnet1',
                                   'tenant_id':
                                   network['network']['tenant_id'],
                                   'gateway_ip': '10.0.20.1'}}
                req = self.new_create_request('subnets', data)
                res = req.get_response(self.api)
                self.assertEqual(400, res.status_int)
                error = self.deserialize(self.fmt, res)
                self.assertEqual('InvalidInput',
                                 error['NeutronError']['type'])
                # Check the client can see the root cause of error.
                self.assertIn(err_msg, error['NeutronError']['message'])
                query_params = "network_id=%s" % net_id
                subnets = self._list('subnets', query_params=query_params)
                self.assertFalse(subnets['subnets'])

    def test_delete_subnet_faulty(self):

        with mock.patch.object(mech_test.TestMechanismDriver,
                               'delete_subnet_postcommit',
                               side_effect=ml2_exc.MechanismDriverError):
            with mock.patch.object(mech_logger.LoggerMechanismDriver,
                                   'delete_subnet_postcommit') as dsp:

                with self.network() as network:
                    data = {'subnet': {'network_id':
                                       network['network']['id'],
                                       'cidr': '10.0.20.0/24',
                                       'ip_version': '4',
                                       'name': 'subnet1',
                                       'tenant_id':
                                       network['network']['tenant_id'],
                                       'gateway_ip': '10.0.20.1'}}
                    subnet_req = self.new_create_request('subnets', data)
                    subnet_res = subnet_req.get_response(self.api)
                    self.assertEqual(201, subnet_res.status_int)
                    subnet = self.deserialize(self.fmt, subnet_res)
                    subnet_id = subnet['subnet']['id']

                    req = self.new_delete_request('subnets', subnet_id)
                    res = req.get_response(self.api)
                    self.assertEqual(204, res.status_int)
                    # Test if other mechanism driver was called
                    self.assertTrue(dsp.called)
                    self._show('subnets', subnet_id,
                               expected_code=webob.exc.HTTPNotFound.code)

    def test_update_subnet_faulty(self):

        err_msg = "Some errors"
        with mock.patch.object(mech_test.TestMechanismDriver,
                               'update_subnet_postcommit',
                               side_effect=(exc.InvalidInput(
                                                error_message=err_msg))):
            with mock.patch.object(mech_logger.LoggerMechanismDriver,
                                   'update_subnet_postcommit') as usp:

                with self.network() as network:
                    data = {'subnet': {'network_id':
                                       network['network']['id'],
                                       'cidr': '10.0.20.0/24',
                                       'ip_version': '4',
                                       'name': 'subnet1',
                                       'tenant_id':
                                       network['network']['tenant_id'],
                                       'gateway_ip': '10.0.20.1'}}
                    subnet_req = self.new_create_request('subnets', data)
                    subnet_res = subnet_req.get_response(self.api)
                    self.assertEqual(201, subnet_res.status_int)
                    subnet = self.deserialize(self.fmt, subnet_res)
                    subnet_id = subnet['subnet']['id']
                    new_name = 'a_brand_new_name'
                    data = {'subnet': {'name': new_name}}
                    req = self.new_update_request('subnets', data, subnet_id)
                    res = req.get_response(self.api)
                    self.assertEqual(400, res.status_int)
                    error = self.deserialize(self.fmt, res)
                    self.assertEqual('InvalidInput',
                                     error['NeutronError']['type'])
                    # Check the client can see the root cause of error.
                    self.assertIn(err_msg, error['NeutronError']['message'])
                    # Test if other mechanism driver was called
                    self.assertTrue(usp.called)
                    subnet = self._show('subnets', subnet_id)
                    self.assertEqual(new_name, subnet['subnet']['name'])

                    self._delete('subnets', subnet['subnet']['id'])

    def test_create_port_faulty(self):

        err_msg = "Some errors"
        with mock.patch.object(mech_test.TestMechanismDriver,
                               'create_port_postcommit',
                               side_effect=(exc.InvalidInput(
                                                error_message=err_msg))):

            with self.network() as network:
                net_id = network['network']['id']
                data = {'port': {'network_id': net_id,
                                 'tenant_id':
                                 network['network']['tenant_id'],
                                 'name': 'port1',
                                 'admin_state_up': 1,
                                 'fixed_ips': []}}
                req = self.new_create_request('ports', data)
                res = req.get_response(self.api)
                self.assertEqual(400, res.status_int)
                error = self.deserialize(self.fmt, res)
                self.assertEqual('InvalidInput',
                                 error['NeutronError']['type'])
                # Check the client can see the root cause of error.
                self.assertIn(err_msg, error['NeutronError']['message'])
                query_params = "network_id=%s" % net_id
                ports = self._list('ports', query_params=query_params)
                self.assertFalse(ports['ports'])

    def test_update_port_faulty(self):

        with mock.patch.object(mech_test.TestMechanismDriver,
                               'update_port_postcommit',
                               side_effect=ml2_exc.MechanismDriverError):
            with mock.patch.object(mech_logger.LoggerMechanismDriver,
                                   'update_port_postcommit') as upp:

                with self.network() as network:
                    data = {'port': {'network_id': network['network']['id'],
                                     'tenant_id':
                                     network['network']['tenant_id'],
                                     'name': 'port1',
                                     'admin_state_up': 1,
                                     'fixed_ips': []}}
                    port_req = self.new_create_request('ports', data)
                    port_res = port_req.get_response(self.api)
                    self.assertEqual(201, port_res.status_int)
                    port = self.deserialize(self.fmt, port_res)
                    port_id = port['port']['id']

                    new_name = 'a_brand_new_name'
                    data = {'port': {'name': new_name}}
                    req = self.new_update_request('ports', data, port_id)
                    res = req.get_response(self.api)
                    self.assertEqual(200, res.status_int)
                    # Test if other mechanism driver was called
                    self.assertTrue(upp.called)
                    port = self._show('ports', port_id)
                    self.assertEqual(new_name, port['port']['name'])

                    self._delete('ports', port['port']['id'])

    def test_update_distributed_router_interface_port(self):
        """Test validate distributed router interface update succeeds."""
        host_id = 'host'
        binding = models.DistributedPortBinding(
                            port_id='port_id',
                            host=host_id,
                            router_id='old_router_id',
                            vif_type=portbindings.VIF_TYPE_OVS,
                            vnic_type=portbindings.VNIC_NORMAL,
                            status=constants.PORT_STATUS_DOWN)
        with mock.patch.object(
            mech_test.TestMechanismDriver,
            'update_port_postcommit',
            side_effect=ml2_exc.MechanismDriverError) as port_post,\
                mock.patch.object(
                    mech_test.TestMechanismDriver,
                    'update_port_precommit') as port_pre,\
                mock.patch.object(
                    ml2_db, 'get_distributed_port_bindings') as dist_bindings:
                dist_bindings.return_value = [binding]
                port_pre.return_value = True
                with self.network() as network:
                    with self.subnet(network=network) as subnet:
                        subnet_id = subnet['subnet']['id']
                        data = {'port': {
                            'network_id': network['network']['id'],
                            'tenant_id':
                            network['network']['tenant_id'],
                            'name': 'port1',
                            'device_owner':
                            constants.DEVICE_OWNER_DVR_INTERFACE,
                            'admin_state_up': 1,
                            'fixed_ips':
                            [{'subnet_id': subnet_id}]}}
                        port_req = self.new_create_request('ports', data)
                        port_res = port_req.get_response(self.api)
                        self.assertEqual(201, port_res.status_int)
                        port = self.deserialize(self.fmt, port_res)
                        port_id = port['port']['id']
                        new_name = 'a_brand_new_name'
                        data = {'port': {'name': new_name}}
                        req = self.new_update_request('ports', data, port_id)
                        res = req.get_response(self.api)
                        self.assertEqual(200, res.status_int)
                        self.assertTrue(dist_bindings.called)
                        self.assertTrue(port_pre.called)
                        self.assertTrue(port_post.called)
                        port = self._show('ports', port_id)
                        self.assertEqual(new_name, port['port']['name'])


class TestML2PluggableIPAM(test_ipam.UseIpamMixin, TestMl2SubnetsV2):
    def test_create_subnet_delete_subnet_call_ipam_driver(self):
        driver = 'neutron.ipam.drivers.neutrondb_ipam.driver.NeutronDbPool'
        gateway_ip = '10.0.0.1'
        cidr = '10.0.0.0/24'
        with mock.patch(driver) as driver_mock:
            request = mock.Mock()
            request.subnet_id = uuidutils.generate_uuid()
            request.subnet_cidr = cidr
            request.allocation_pools = []
            request.gateway_ip = gateway_ip
            request.tenant_id = uuidutils.generate_uuid()

            ipam_subnet = mock.Mock()
            ipam_subnet.get_details.return_value = request
            driver_mock().allocate_subnet.return_value = ipam_subnet

            self._test_create_subnet(gateway_ip=gateway_ip, cidr=cidr)

            driver_mock().allocate_subnet.assert_called_with(mock.ANY)
            driver_mock().remove_subnet.assert_called_with(request.subnet_id)

    def test_delete_subnet_deallocates_slaac_correctly(self):
        driver = 'neutron.ipam.drivers.neutrondb_ipam.driver.NeutronDbPool'
        with self.network() as network:
            with self.subnet(network=network,
                             cidr='2001:100::0/64',
                             ip_version=6,
                             ipv6_ra_mode=constants.IPV6_SLAAC) as subnet:
                with self.port(subnet=subnet) as port:
                    with mock.patch(driver) as driver_mock:
                        # Validate that deletion of SLAAC allocation happens
                        # via IPAM interface, i.e. ipam_subnet.deallocate is
                        # called prior to subnet deletiong from db.
                        self._delete('subnets', subnet['subnet']['id'])
                        dealloc = driver_mock().get_subnet().deallocate
                        dealloc.assert_called_with(
                            port['port']['fixed_ips'][0]['ip_address'])
                        driver_mock().remove_subnet.assert_called_with(
                            subnet['subnet']['id'])


class TestMl2PluginCreateUpdateDeletePort(base.BaseTestCase):

    def setUp(self):
        super(TestMl2PluginCreateUpdateDeletePort, self).setUp()
        # TODO(ihrachys): revisit plugin setup once we decouple
        # neutron.objects.db.api from core plugin instance
        self.setup_coreplugin(PLUGIN_NAME, load_plugins=False)
        self.context = mock.MagicMock()
        self.context.session.is_active = False
        self.notify_p = mock.patch('neutron.callbacks.registry.notify')
        self.notify = self.notify_p.start()

    def _ensure_transaction_is_closed(self):
        transaction = self.context.session.begin(subtransactions=True)
        enter = transaction.__enter__.call_count
        exit = transaction.__exit__.call_count
        self.assertEqual(enter, exit)

    def _create_plugin_for_create_update_port(self):
        plugin = ml2_plugin.Ml2Plugin()
        directory.add_plugin(constants.CORE, plugin)
        plugin.extension_manager = mock.Mock()
        plugin.type_manager = mock.Mock()
        plugin.mechanism_manager = mock.Mock()
        plugin.notifier = mock.Mock()
        plugin._check_mac_update_allowed = mock.Mock(return_value=True)
        plugin._extend_availability_zone = mock.Mock()

        self.notify.side_effect = (
            lambda r, e, t, **kwargs: self._ensure_transaction_is_closed())

        return plugin

    def test_create_port_rpc_outside_transaction(self):
        with mock.patch.object(ml2_plugin.Ml2Plugin, '__init__') as init,\
                mock.patch.object(base_plugin.NeutronDbPluginV2,
                                  '_make_port_dict') as make_port, \
                mock.patch.object(base_plugin.NeutronDbPluginV2,
                                  'update_port'),\
                mock.patch.object(base_plugin.NeutronDbPluginV2,
                                  'create_port_db'),\
                mock.patch.object(ml2_plugin.Ml2Plugin,
                                  '_get_network_mtu'):
            init.return_value = None

            new_port = mock.MagicMock()
            make_port.return_value = new_port
            plugin = self._create_plugin_for_create_update_port()

            plugin.create_port(self.context, mock.MagicMock())

            kwargs = {'context': self.context, 'port': new_port}
            self.notify.assert_called_once_with('port', 'after_create',
                plugin, **kwargs)

    def test_update_port_rpc_outside_transaction(self):
        port_id = 'fake_id'
        net_id = 'mynet'
        original_port_db = models_v2.Port(
            id=port_id,
            tenant_id='tenant',
            network_id=net_id,
            mac_address='08:00:01:02:03:04',
            admin_state_up=True,
            status='ACTIVE',
            device_id='vm_id',
            device_owner=DEVICE_OWNER_COMPUTE)

        binding = mock.Mock()
        binding.port_id = port_id
        binding.host = 'vm_host'
        binding.vnic_type = portbindings.VNIC_NORMAL
        binding.profile = ''
        binding.vif_type = ''
        binding.vif_details = ''

        with mock.patch.object(ml2_plugin.Ml2Plugin, '__init__') as init,\
                mock.patch.object(ml2_db, 'get_locked_port_and_binding',
                                  return_value=(original_port_db, binding)),\
                mock.patch.object(base_plugin.NeutronDbPluginV2,
                                  'update_port') as db_update_port,\
                mock.patch.object(ml2_plugin.Ml2Plugin,
                                  '_get_network_mtu'):
            init.return_value = None
            updated_port = mock.MagicMock()
            db_update_port.return_value = updated_port
            plugin = self._create_plugin_for_create_update_port()
            original_port = plugin._make_port_dict(original_port_db)

            res = plugin.update_port(self.context, port_id, mock.MagicMock())

            first_update = {
                'context': self.context,
                'port': updated_port,
                'mac_address_updated': True,
                'original_port': original_port,
            }
            bind_update = {
                'context': self.context,
                'port': res,
                'mac_address_updated': False,
                'original_port': original_port,
            }
            expected = [
                mock.call('port', 'after_update', plugin, **first_update),
                mock.call('port', 'after_update', plugin, **bind_update)
            ]
            self.notify.assert_has_calls(expected)

    def test_notify_outside_of_delete_transaction(self):
        self.notify.side_effect = (
            lambda r, e, t, **kwargs: self._ensure_transaction_is_closed())
        l3plugin = mock.Mock()
        l3plugin.supported_extension_aliases = [
            'router', constants.L3_AGENT_SCHEDULER_EXT_ALIAS,
            constants.L3_DISTRIBUTED_EXT_ALIAS
        ]
        with mock.patch.object(ml2_plugin.Ml2Plugin,
                               '__init__',
                               return_value=None),\
                mock.patch.object(directory,
                                  'get_plugins',
                                  return_value={constants.L3: l3plugin}),\
                mock.patch.object(ml2_plugin.Ml2Plugin,
                                  '_get_network_mtu'):
            plugin = self._create_plugin_for_create_update_port()
            # Set backend manually here since __init__ was mocked
            plugin.set_ipam_backend()
            # deleting the port will call registry.notify, which will
            # run the transaction balancing function defined in this test
            plugin.delete_port(self.context, 'fake_id')
            self.assertTrue(self.notify.call_count)


class TestTransactionGuard(Ml2PluginV2TestCase):
    def test_delete_network_guard(self):
        plugin = directory.get_plugin()
        ctx = context.get_admin_context()
        with ctx.session.begin(subtransactions=True):
            with testtools.ExpectedException(RuntimeError):
                plugin.delete_network(ctx, 'id')

    def test_delete_subnet_guard(self):
        plugin = directory.get_plugin()
        ctx = context.get_admin_context()
        with ctx.session.begin(subtransactions=True):
            with testtools.ExpectedException(RuntimeError):
                plugin.delete_subnet(ctx, 'id')


class TestML2Segments(Ml2PluginV2TestCase):

    def _reserve_segment(self, network, seg_id=None):
        segment = {'id': 'fake_id',
                   'network_id': network['network']['id'],
                   'tenant_id': network['network']['tenant_id'],
                   driver_api.NETWORK_TYPE: 'vlan',
                   driver_api.PHYSICAL_NETWORK: self.physnet}
        if seg_id:
            segment[driver_api.SEGMENTATION_ID] = seg_id

        self.driver._handle_segment_change(
            mock.ANY, events.PRECOMMIT_CREATE, segments_plugin.Plugin(),
            self.context, segment)

        if seg_id:
            # Assert it is not changed
            self.assertEqual(seg_id, segment[driver_api.SEGMENTATION_ID])
        else:
            self.assertTrue(segment[driver_api.SEGMENTATION_ID] > 0)

        return segment

    def test_reserve_segment_success_with_partial_segment(self):
        with self.network() as network:
            self._reserve_segment(network)

    def test_reserve_segment_fail_with_duplicate_param(self):
        with self.network() as network:
            self._reserve_segment(network, 10)

            self.assertRaises(
                exc.VlanIdInUse, self._reserve_segment, network, 10)

    def test_create_network_mtu_on_precommit(self):
        with mock.patch.object(mech_test.TestMechanismDriver,
                        'create_network_precommit') as bmp:
            with mock.patch.object(
                self.driver, '_get_network_mtu') as mtu:
                mtu.return_value = 1100
                with self.network() as network:
                    self.assertIn('mtu', network['network'])
            all_args = bmp.call_args_list
            mech_context = all_args[0][0][0]
            self.assertEqual(1100, mech_context.__dict__['_network']['mtu'])

    def test_provider_info_update_network(self):
        with self.network() as network:
            network_id = network['network']['id']
            plugin = directory.get_plugin()
            updated_network = plugin.update_network(
                self.context, network_id, {'network': {'name': 'test-net'}})
            self.assertIn('provider:network_type', updated_network)
            self.assertIn('provider:physical_network', updated_network)
            self.assertIn('provider:segmentation_id', updated_network)

    def test_reserve_segment_update_network_mtu(self):
        with self.network() as network:
            network_id = network['network']['id']
            with mock.patch.object(
                self.driver, '_get_network_mtu') as mtu:
                mtu.return_value = 100
                self._reserve_segment(network)
                updated_network = self.driver.get_network(self.context,
                                                          network_id)
                self.assertEqual(100, updated_network[driver_api.MTU])

                mtu.return_value = 200
                self._reserve_segment(network)
                updated_network = self.driver.get_network(self.context,
                                                          network_id)
                self.assertEqual(200, updated_network[driver_api.MTU])

    def _test_nofity_mechanism_manager(self, event):
        seg1 = {driver_api.NETWORK_TYPE: 'vlan',
                driver_api.PHYSICAL_NETWORK: self.physnet,
                driver_api.SEGMENTATION_ID: 1000}
        seg2 = {driver_api.NETWORK_TYPE: 'vlan',
                driver_api.PHYSICAL_NETWORK: self.physnet,
                driver_api.SEGMENTATION_ID: 1001}
        seg3 = {driver_api.NETWORK_TYPE: 'vlan',
                driver_api.PHYSICAL_NETWORK: self.physnet,
                driver_api.SEGMENTATION_ID: 1002}
        with self.network() as network:
            network = network['network']

        for stale_seg in segments_db.get_network_segments(self.context,
                                                          network['id']):
            segments_db.delete_network_segment(self.context, stale_seg['id'])

        for seg in [seg1, seg2, seg3]:
            seg['network_id'] = network['id']
            segments_db.add_network_segment(self.context, network['id'], seg)

        self.net_context = None

        def record_network_context(net_context):
            self.net_context = net_context

        with mock.patch.object(managers.MechanismManager,
                               'update_network_precommit',
                               side_effect=record_network_context):
            self.driver._handle_segment_change(
                mock.ANY, event, segments_plugin.Plugin(), self.context, seg1)
            # Make sure the mechanism manager can get the right amount of
            # segments of network
            self.assertEqual(3, len(self.net_context.current[mpnet.SEGMENTS]))

    def test_reserve_segment_nofity_mechanism_manager(self):
        self._test_nofity_mechanism_manager(events.PRECOMMIT_CREATE)

    def test_release_segment(self):
        with self.network() as network:
            segment = self._reserve_segment(network, 10)
            segment['network_id'] = network['network']['id']
            self.driver._handle_segment_change(
                mock.ANY, events.PRECOMMIT_DELETE, mock.ANY,
                self.context, segment)
            # Check that the segment_id is not reserved
            segment = self._reserve_segment(
                network, segment[driver_api.SEGMENTATION_ID])

    def test_release_segment_nofity_mechanism_manager(self):
        self._test_nofity_mechanism_manager(events.PRECOMMIT_DELETE)

    def test_prevent_delete_segment_with_tenant_port(self):
        fake_owner_compute = constants.DEVICE_OWNER_COMPUTE_PREFIX + 'fake'
        ml2_db.subscribe()
        plugin = directory.get_plugin()
        with self.port(device_owner=fake_owner_compute) as port:
            binding = ml2_db.get_locked_port_and_binding(self.context,
                                                         port['port']['id'])[1]
            binding['host'] = 'host-ovs-no_filter'
            mech_context = driver_context.PortContext(
                plugin, self.context, port['port'],
                plugin.get_network(self.context, port['port']['network_id']),
                binding, None)
            plugin._bind_port_if_needed(mech_context)
            segment = segments_db.get_network_segments(
                self.context, port['port']['network_id'])[0]
            segment['network_id'] = port['port']['network_id']
            self.assertRaises(c_exc.CallbackFailure, registry.notify,
                              resources.SEGMENT, events.BEFORE_DELETE,
                              mock.ANY,
                              context=self.context, segment=segment)
            exist_port = self._show('ports', port['port']['id'])
            self.assertEqual(port['port']['id'], exist_port['port']['id'])
