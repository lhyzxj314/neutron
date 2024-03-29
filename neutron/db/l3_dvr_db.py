# Copyright (c) 2014 OpenStack Foundation.  All rights reserved.
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
import collections

from neutron_lib.api import validators
from neutron_lib import constants as const
from neutron_lib import exceptions as n_exc
from neutron_lib.plugins import directory
from oslo_config import cfg
from oslo_log import helpers as log_helper
from oslo_log import log as logging
from oslo_utils import excutils
import six

from neutron._i18n import _, _LE, _LI, _LW
from neutron.callbacks import events
from neutron.callbacks import exceptions
from neutron.callbacks import registry
from neutron.callbacks import resources
from neutron.common import constants as l3_const
from neutron.common import utils as n_utils
from neutron.db import api as db_api
from neutron.db import l3_attrs_db
from neutron.db import l3_db
from neutron.db.models import allowed_address_pair as aap_models
from neutron.db.models import l3 as l3_models
from neutron.db.models import l3agent as rb_model
from neutron.db import models_v2
from neutron.extensions import l3
from neutron.extensions import portbindings
from neutron.ipam import utils as ipam_utils
from neutron.plugins.common import utils as p_utils


LOG = logging.getLogger(__name__)
router_distributed_opts = [
    cfg.BoolOpt('router_distributed',
                default=False,
                help=_("System-wide flag to determine the type of router "
                       "that tenants can create. Only admin can override.")),
]
cfg.CONF.register_opts(router_distributed_opts)


class L3_NAT_with_dvr_db_mixin(l3_db.L3_NAT_db_mixin,
                               l3_attrs_db.ExtraAttributesMixin):
    """Mixin class to enable DVR support."""

    router_device_owners = (
        l3_db.L3_NAT_db_mixin.router_device_owners +
        (const.DEVICE_OWNER_DVR_INTERFACE,
         const.DEVICE_OWNER_ROUTER_SNAT,
         const.DEVICE_OWNER_AGENT_GW))

    def __new__(cls, *args, **kwargs):
        n = super(L3_NAT_with_dvr_db_mixin, cls).__new__(cls, *args, **kwargs)
        registry.subscribe(n._create_dvr_floating_gw_port,
                           resources.FLOATING_IP, events.AFTER_UPDATE)
        registry.subscribe(n._create_snat_interfaces_after_change,
                           resources.ROUTER, events.AFTER_UPDATE)
        registry.subscribe(n._create_snat_interfaces_after_change,
                           resources.ROUTER, events.AFTER_CREATE)
        registry.subscribe(n._delete_dvr_internal_ports,
                           resources.ROUTER_GATEWAY, events.AFTER_DELETE)
        registry.subscribe(n._set_distributed_flag,
                           resources.ROUTER, events.PRECOMMIT_CREATE)
        registry.subscribe(n._handle_distributed_migration,
                           resources.ROUTER, events.PRECOMMIT_UPDATE)
        registry.subscribe(n._add_csnat_on_interface_create,
                           resources.ROUTER_INTERFACE, events.BEFORE_CREATE)
        registry.subscribe(n._update_snat_v6_addrs_after_intf_update,
                           resources.ROUTER_INTERFACE, events.AFTER_CREATE)
        registry.subscribe(n._cleanup_after_interface_removal,
                           resources.ROUTER_INTERFACE, events.AFTER_DELETE)
        return n

    def _set_distributed_flag(self, resource, event, trigger, context,
                              router, router_db, **kwargs):
        """Event handler to set distributed flag on creation."""
        dist = is_distributed_router(router)
        router['distributed'] = dist
        self.set_extra_attr_value(context, router_db, 'distributed', dist)

    def _validate_router_migration(self, context, router_db, router_res):
        """Allow transition only when admin_state_up=False"""
        original_distributed_state = router_db.extra_attributes.distributed
        requested_distributed_state = router_res.get('distributed', None)

        distributed_changed = (
            requested_distributed_state is not None and
            requested_distributed_state != original_distributed_state)
        if not distributed_changed:
            return False
        if router_db.admin_state_up:
            msg = _("Cannot change the 'distributed' attribute of active "
                    "routers. Please set router admin_state_up to False "
                    "prior to upgrade")
            raise n_exc.BadRequest(resource='router', msg=msg)

        # Notify advanced services of the imminent state transition
        # for the router.
        try:
            kwargs = {'context': context, 'router': router_db}
            registry.notify(
                resources.ROUTER, events.BEFORE_UPDATE, self, **kwargs)
        except exceptions.CallbackFailure as e:
            with excutils.save_and_reraise_exception():
                # NOTE(armax): preserve old check's behavior
                if len(e.errors) == 1:
                    raise e.errors[0].error
                raise l3.RouterInUse(router_id=router_db['id'],
                                     reason=e)
        return True

    def _handle_distributed_migration(self, resource, event, trigger, context,
                                      router_id, router, router_db, **kwargs):
        """Event handler for router update migration to distributed."""
        if not self._validate_router_migration(context, router_db, router):
            return

        migrating_to_distributed = (
            not router_db.extra_attributes.distributed and
            router.get('distributed') is True)

        if migrating_to_distributed:
            self._migrate_router_ports(
                context, router_db,
                old_owner=const.DEVICE_OWNER_ROUTER_INTF,
                new_owner=const.DEVICE_OWNER_DVR_INTERFACE)
            self.set_extra_attr_value(context, router_db, 'distributed', True)
        else:
            self._migrate_router_ports(
                context, router_db,
                old_owner=const.DEVICE_OWNER_DVR_INTERFACE,
                new_owner=const.DEVICE_OWNER_ROUTER_INTF)
            self.set_extra_attr_value(context, router_db, 'distributed', False)

        cur_agents = self.list_l3_agents_hosting_router(
            context, router_db['id'])['agents']
        for agent in cur_agents:
            self._unbind_router(context, router_db['id'], agent['id'])

    def _create_snat_interfaces_after_change(self, resource, event, trigger,
                                             context, router_id, router,
                                             request_attrs, router_db,
                                             **kwargs):
        if not router.get(l3.EXTERNAL_GW_INFO) or not router['distributed']:
            # we don't care if it's not distributed or not attached to an
            # external network
            return
        if event == events.AFTER_UPDATE:
            # after an update, we check to see if it was a migration or a
            # gateway attachment
            old_router = kwargs['old_router']
            do_create = (not old_router['distributed'] or
                         not old_router.get(l3.EXTERNAL_GW_INFO))
            if not do_create:
                return
        if not self._create_snat_intf_ports_if_not_exists(
            context.elevated(), router_db):
            LOG.debug("SNAT interface ports not created: %s",
                      router_db['id'])
        return router_db

    def _delete_dvr_internal_ports(self, event, trigger, resource,
                                   context, router, network_id,
                                   new_network_id, **kwargs):
        """
        GW port AFTER_DELETE event handler to cleanup DVR ports.

        This event is emitted when a router gateway port is being deleted,
        so go ahead and delete the csnat ports and the floatingip
        agent gateway port associated with the dvr router.
        """

        if not is_distributed_router(router):
            return
        if not new_network_id:
            self.delete_csnat_router_interface_ports(context.elevated(),
                                                     router)
        # NOTE(Swami): Delete the Floatingip agent gateway port
        # on all hosts when it is the last gateway port in the
        # given external network.
        filters = {'network_id': [network_id],
                   'device_owner': [const.DEVICE_OWNER_ROUTER_GW]}
        ext_net_gw_ports = self._core_plugin.get_ports(
            context.elevated(), filters)
        if not ext_net_gw_ports:
            self.delete_floatingip_agent_gateway_port(
                context.elevated(), None, network_id)
            # Send the information to all the L3 Agent hosts
            # to clean up the fip namespace as it is no longer required.
            self.l3_rpc_notifier.delete_fipnamespace_for_ext_net(
                context, network_id)

    def _get_device_owner(self, context, router=None):
        """Get device_owner for the specified router."""
        router_is_uuid = isinstance(router, six.string_types)
        if router_is_uuid:
            router = self._get_router(context, router)
        if is_distributed_router(router):
            return const.DEVICE_OWNER_DVR_INTERFACE
        return super(L3_NAT_with_dvr_db_mixin,
                     self)._get_device_owner(context, router)

    def _get_ports_for_allowed_address_pair_ip(
        self, context, network_id, fixed_ip):
        """Return all active ports associated with the allowed_addr_pair ip."""
        query = context.session.query(
            models_v2.Port).filter(
                models_v2.Port.id == aap_models.AllowedAddressPair.port_id,
                aap_models.AllowedAddressPair.ip_address == fixed_ip,
                models_v2.Port.network_id == network_id,
                models_v2.Port.admin_state_up == True)  # noqa
        return query.all()

    def _create_dvr_floating_gw_port(self, resource, event, trigger, context,
                                     router_id, fixed_port_id, floating_ip_id,
                                     floating_network_id, fixed_ip_address,
                                     **kwargs):
        """Create floating agent gw port for DVR.

        Floating IP Agent gateway port will be created when a
        floatingIP association happens.
        """
        associate_fip = fixed_port_id and floating_ip_id
        if associate_fip and router_id:
            admin_ctx = context.elevated()
            router_dict = self.get_router(admin_ctx, router_id)
            # Check if distributed router and then create the
            # FloatingIP agent gateway port
            if router_dict.get('distributed'):
                hostid = self._get_dvr_service_port_hostid(context,
                                                           fixed_port_id)
                if hostid:
                    # FIXME (Swami): This FIP Agent Gateway port should be
                    # created only once and there should not be a duplicate
                    # for the same host. Until we find a good solution for
                    # augmenting multiple server requests we should use the
                    # existing flow.
                    fip_agent_port = (
                        self.create_fip_agent_gw_port_if_not_exists(
                            admin_ctx, floating_network_id, hostid))
                    LOG.debug("FIP Agent gateway port: %s", fip_agent_port)
                else:
                    # If not hostid check if the fixed ip provided has to
                    # deal with allowed_address_pairs for a given service
                    # port. Get the port_dict, inherit the service port host
                    # and device owner(if it does not exist).
                    port = self._core_plugin.get_port(
                        admin_ctx, fixed_port_id)
                    allowed_device_owners = (
                        n_utils.get_dvr_allowed_address_pair_device_owners())
                    # NOTE: We just need to deal with ports that do not
                    # have a device_owner and ports that are owned by the
                    # dvr service ports except for the compute port and
                    # dhcp port.
                    if (port['device_owner'] == "" or
                        port['device_owner'] in allowed_device_owners):
                        addr_pair_active_service_port_list = (
                            self._get_ports_for_allowed_address_pair_ip(
                                admin_ctx, port['network_id'],
                                fixed_ip_address))
                        if not addr_pair_active_service_port_list:
                            return
                        if len(addr_pair_active_service_port_list) > 1:
                            LOG.warning(_LW("Multiple active ports associated "
                                            "with the allowed_address_pairs."))
                            return
                        self._inherit_service_port_and_arp_update(
                            context, addr_pair_active_service_port_list[0],
                            port)

    def _inherit_service_port_and_arp_update(
        self, context, service_port, allowed_address_port):
        """Function inherits port host bindings for allowed_address_pair."""
        service_port_dict = self._core_plugin._make_port_dict(service_port)
        address_pair_list = service_port_dict.get('allowed_address_pairs')
        for address_pair in address_pair_list:
            updated_port = (
                self.update_unbound_allowed_address_pair_port_binding(
                    context, service_port_dict,
                    address_pair,
                    address_pair_port=allowed_address_port))
            if not updated_port:
                LOG.warning(_LW("Allowed_address_pair port update failed: %s"),
                            updated_port)
            self.update_arp_entry_for_dvr_service_port(context,
                                                       service_port_dict)

    def _get_floatingip_on_port(self, context, port_id=None):
        """Helper function to retrieve the fip associated with port."""
        fip_qry = context.session.query(l3_models.FloatingIP)
        floating_ip = fip_qry.filter_by(fixed_port_id=port_id)
        return floating_ip.first()

    @db_api.retry_if_session_inactive()
    def _add_csnat_on_interface_create(self, resource, event, trigger,
                                       context, router_db, port, **kwargs):
        """Event handler to for csnat port creation on interface creation."""
        if not router_db.extra_attributes.distributed or not router_db.gw_port:
            return
        admin_context = context.elevated()
        self._add_csnat_router_interface_port(
            admin_context, router_db, port['network_id'],
            port['fixed_ips'][-1]['subnet_id'])

    @db_api.retry_if_session_inactive()
    def _update_snat_v6_addrs_after_intf_update(self, resource, event, triger,
                                                context, subnets, port,
                                                router_id, new_interface,
                                                **kwargs):
        if new_interface:
            # _add_csnat_on_interface_create handler deals with new ports
            return
        # if not a new interface, the interface was added to a new subnet,
        # which is the first in this list
        subnet = subnets[0]
        if not subnet or subnet['ip_version'] != 6:
            return
        # NOTE: For IPv6 additional subnets added to the same
        # network we need to update the CSNAT port with respective
        # IPv6 subnet
        # Add new prefix to an existing ipv6 csnat port with the
        # same network id if one exists
        admin_ctx = context.elevated()
        router = self._get_router(admin_ctx, router_id)
        cs_port = self._find_v6_router_port_by_network_and_device_owner(
            router, subnet['network_id'], const.DEVICE_OWNER_ROUTER_SNAT)
        if not cs_port:
            return
        new_fixed_ip = {'subnet_id': subnet['id']}
        fixed_ips = list(cs_port['fixed_ips'])
        fixed_ips.append(new_fixed_ip)
        try:
            updated_port = self._core_plugin.update_port(
                admin_ctx, cs_port['id'], {'port': {'fixed_ips': fixed_ips}})
        except Exception:
            with excutils.save_and_reraise_exception():
                # we need to try to undo the updated router
                # interface from above so it's not out of sync
                # with the csnat port.
                # TODO(kevinbenton): switch to taskflow to manage
                # these rollbacks.
                @db_api.retry_db_errors
                def revert():
                    # TODO(kevinbenton): even though we get the
                    # port each time, there is a potential race
                    # where we update the port with stale IPs if
                    # another interface operation is occurring at
                    # the same time. This can be fixed in the
                    # future with a compare-and-swap style update
                    # using the revision number of the port.
                    p = self._core_plugin.get_port(admin_ctx, port['id'])
                    rollback_fixed_ips = [ip for ip in p['fixed_ips']
                                          if ip['subnet_id'] != subnet['id']]
                    upd = {'port': {'fixed_ips': rollback_fixed_ips}}
                    self._core_plugin.update_port(admin_ctx, port['id'], upd)
                try:
                    revert()
                except Exception:
                    LOG.exception(_LE("Failed to revert change "
                                      "to router port %s."),
                                  port['id'])
        LOG.debug("CSNAT port updated for IPv6 subnet: %s", updated_port)

    def _find_v6_router_port_by_network_and_device_owner(
        self, router, net_id, device_owner):
        for port in router.attached_ports:
            p = port['port']
            if (p['network_id'] == net_id and
                p['device_owner'] == device_owner and
                self._port_has_ipv6_address(p)):
                return self._core_plugin._make_port_dict(p)

    def _check_for_multiprefix_csnat_port_and_update(
        self, context, router, network_id, subnet_id):
        """Checks if the csnat port contains multiple ipv6 prefixes.

        If the csnat port contains multiple ipv6 prefixes for the given
        network when a router interface is deleted, make sure we don't
        delete the port when a single subnet is deleted and just update
        it with the right fixed_ip.
        This function returns true if it is a multiprefix port.
        """
        if router.gw_port:
            # If router has a gateway port, check if it has IPV6 subnet
            cs_port = (
                self._find_v6_router_port_by_network_and_device_owner(
                    router, network_id, const.DEVICE_OWNER_ROUTER_SNAT))
            if cs_port:
                fixed_ips = (
                    [fixedip for fixedip in
                        cs_port['fixed_ips']
                        if fixedip['subnet_id'] != subnet_id])

                if len(fixed_ips) == len(cs_port['fixed_ips']):
                    # The subnet being detached from router is not part of
                    # ipv6 router port. No need to update the multiprefix.
                    return False

                if fixed_ips:
                    # multiple prefix port - delete prefix from port
                    self._core_plugin.update_port(
                        context.elevated(),
                        cs_port['id'], {'port': {'fixed_ips': fixed_ips}})
                    return True
        return False

    @db_api.retry_if_session_inactive()
    def _cleanup_after_interface_removal(self, resource, event, trigger,
                                         context, port, interface_info,
                                         router_id, **kwargs):
        """Handler to cleanup distributed resources after intf removal."""
        router = self._get_router(context, router_id)
        if not router.extra_attributes.distributed:
            return

        plugin = directory.get_plugin(const.L3)

        # we calculate which hosts to notify by checking the hosts for
        # the removed port's subnets and then subtract out any hosts still
        # hosting the router for the remaining interfaces
        router_hosts_for_removed = plugin._get_dvr_hosts_for_subnets(
            context, subnet_ids={ip['subnet_id'] for ip in port['fixed_ips']})
        router_hosts_after = plugin._get_dvr_hosts_for_router(
            context, router_id)
        removed_hosts = set(router_hosts_for_removed) - set(router_hosts_after)
        if removed_hosts:
            agents = plugin.get_l3_agents(context,
                                          filters={'host': removed_hosts})
            binding_table = rb_model.RouterL3AgentBinding
            snat_binding = context.session.query(binding_table).filter_by(
                router_id=router_id).first()
            for agent in agents:
                is_this_snat_agent = (
                    snat_binding and snat_binding.l3_agent_id == agent['id'])
                if not is_this_snat_agent:
                    self.l3_rpc_notifier.router_removed_from_agent(
                        context, router_id, agent['host'])
        # if subnet_id not in interface_info, request was to remove by port
        sub_id = (interface_info.get('subnet_id') or
                  port['fixed_ips'][0]['subnet_id'])
        is_multiple_prefix_csport = (
            self._check_for_multiprefix_csnat_port_and_update(
                context, router, port['network_id'], sub_id))
        if not is_multiple_prefix_csport:
            # Single prefix port - go ahead and delete the port
            self.delete_csnat_router_interface_ports(
                context.elevated(), router, subnet_id=sub_id)

    def _get_snat_sync_interfaces(self, context, router_ids):
        """Query router interfaces that relate to list of router_ids."""
        if not router_ids:
            return []
        qry = context.session.query(l3_models.RouterPort)
        qry = qry.filter(
            l3_models.RouterPort.router_id.in_(router_ids),
            l3_models.RouterPort.port_type == const.DEVICE_OWNER_ROUTER_SNAT
        )
        interfaces = collections.defaultdict(list)
        for rp in qry:
            interfaces[rp.router_id].append(
                self._core_plugin._make_port_dict(rp.port))
        LOG.debug("Return the SNAT ports: %s", interfaces)
        return interfaces

    def _build_routers_list(self, context, routers, gw_ports):
        # Perform a single query up front for all routers
        if not routers:
            return []
        router_ids = [r['id'] for r in routers]
        snat_binding = rb_model.RouterL3AgentBinding
        query = (context.session.query(snat_binding).
                 filter(snat_binding.router_id.in_(router_ids))).all()
        bindings = dict((b.router_id, b) for b in query)

        for rtr in routers:
            gw_port_id = rtr['gw_port_id']
            # Collect gw ports only if available
            if gw_port_id and gw_ports.get(gw_port_id):
                rtr['gw_port'] = gw_ports[gw_port_id]
                if 'enable_snat' in rtr[l3.EXTERNAL_GW_INFO]:
                    rtr['enable_snat'] = (
                        rtr[l3.EXTERNAL_GW_INFO]['enable_snat'])

                binding = bindings.get(rtr['id'])
                if not binding:
                    rtr['gw_port_host'] = None
                    LOG.debug('No snat is bound to router %s', rtr['id'])
                    continue

                rtr['gw_port_host'] = binding.l3_agent.host

        return routers

    def _process_routers(self, context, routers):
        routers_dict = {}
        snat_intfs_by_router_id = self._get_snat_sync_interfaces(
            context, [r['id'] for r in routers])
        for router in routers:
            routers_dict[router['id']] = router
            if router['gw_port_id']:
                snat_router_intfs = snat_intfs_by_router_id[router['id']]
                LOG.debug("SNAT ports returned: %s ", snat_router_intfs)
                router[l3_const.SNAT_ROUTER_INTF_KEY] = snat_router_intfs
        return routers_dict

    def _process_floating_ips_dvr(self, context, routers_dict,
                                  floating_ips, host, agent):
        fip_sync_interfaces = None
        LOG.debug("FIP Agent : %s ", agent.id)
        for floating_ip in floating_ips:
            router = routers_dict.get(floating_ip['router_id'])
            if router:
                router_floatingips = router.get(const.FLOATINGIP_KEY, [])
                if router['distributed']:
                    if (floating_ip.get('host', None) != host and
                        floating_ip.get('dest_host') is None):
                        continue
                    LOG.debug("Floating IP host: %s", floating_ip['host'])
                router_floatingips.append(floating_ip)
                router[const.FLOATINGIP_KEY] = router_floatingips
                if not fip_sync_interfaces:
                    fip_sync_interfaces = self._get_fip_sync_interfaces(
                        context, agent.id)
                    LOG.debug("FIP Agent ports: %s", fip_sync_interfaces)
                router[l3_const.FLOATINGIP_AGENT_INTF_KEY] = (
                    fip_sync_interfaces)

    def _get_fip_sync_interfaces(self, context, fip_agent_id):
        """Query router interfaces that relate to list of router_ids."""
        if not fip_agent_id:
            return []
        filters = {'device_id': [fip_agent_id],
                   'device_owner': [const.DEVICE_OWNER_AGENT_GW]}
        interfaces = self._core_plugin.get_ports(context.elevated(), filters)
        LOG.debug("Return the FIP ports: %s ", interfaces)
        return interfaces

    @log_helper.log_method_call
    def _get_dvr_sync_data(self, context, host, agent, router_ids=None,
                          active=None):
        routers, interfaces, floating_ips = self._get_router_info_list(
            context, router_ids=router_ids, active=active,
            device_owners=const.ROUTER_INTERFACE_OWNERS)
        dvr_router_ids = set(router['id'] for router in routers
                             if is_distributed_router(router))
        floating_ip_port_ids = [fip['port_id'] for fip in floating_ips
                                if fip['router_id'] in dvr_router_ids]
        if floating_ip_port_ids:
            port_filter = {'id': floating_ip_port_ids}
            ports = self._core_plugin.get_ports(context, port_filter)
            port_dict = {}
            for port in ports:
                # Make sure that we check for cases were the port
                # might be in a pre-live migration state or also
                # check for the portbinding profile 'migrating_to'
                # key for the host.
                port_profile = port.get(portbindings.PROFILE)
                port_in_migration = (
                    port_profile and
                    port_profile.get('migrating_to') == host)
                if (port[portbindings.HOST_ID] == host or port_in_migration):
                    port_dict.update({port['id']: port})
            # Add the port binding host to the floatingip dictionary
            for fip in floating_ips:
                vm_port = port_dict.get(fip['port_id'], None)
                if vm_port:
                    fip['host'] = self._get_dvr_service_port_hostid(
                        context, fip['port_id'], port=vm_port)
                    fip['dest_host'] = (
                        self._get_dvr_migrating_service_port_hostid(
                            context, fip['port_id'], port=vm_port))
        routers_dict = self._process_routers(context, routers)
        self._process_floating_ips_dvr(context, routers_dict,
                                       floating_ips, host, agent)
        ports_to_populate = []
        for router in routers_dict.values():
            if router.get('gw_port'):
                ports_to_populate.append(router['gw_port'])
            if router.get(l3_const.FLOATINGIP_AGENT_INTF_KEY):
                ports_to_populate += router[l3_const.FLOATINGIP_AGENT_INTF_KEY]
            if router.get(l3_const.SNAT_ROUTER_INTF_KEY):
                ports_to_populate += router[l3_const.SNAT_ROUTER_INTF_KEY]
        ports_to_populate += interfaces
        self._populate_mtu_and_subnets_for_ports(context, ports_to_populate)
        self._process_interfaces(routers_dict, interfaces)
        return list(routers_dict.values())

    def _get_dvr_service_port_hostid(self, context, port_id, port=None):
        """Returns the portbinding host_id for dvr service port."""
        port_db = port or self._core_plugin.get_port(context, port_id)
        device_owner = port_db['device_owner'] if port_db else ""
        if n_utils.is_dvr_serviced(device_owner):
            return port_db[portbindings.HOST_ID]

    def _get_dvr_migrating_service_port_hostid(
        self, context, port_id, port=None):
        """Returns the migrating host_id from the migrating profile."""
        port_db = port or self._core_plugin.get_port(context, port_id)
        port_profile = port_db.get(portbindings.PROFILE)
        port_dest_host = None
        if port_profile:
            port_dest_host = port_profile.get('migrating_to')
        device_owner = port_db['device_owner'] if port_db else ""
        if n_utils.is_dvr_serviced(device_owner):
            return port_dest_host

    def _get_agent_gw_ports_exist_for_network(
            self, context, network_id, host, agent_id):
        """Return agent gw port if exist, or None otherwise."""
        if not network_id:
            LOG.debug("Network not specified")
            return

        filters = {
            'network_id': [network_id],
            'device_id': [agent_id],
            'device_owner': [const.DEVICE_OWNER_AGENT_GW]
        }
        ports = self._core_plugin.get_ports(context, filters)
        if ports:
            return ports[0]

    def delete_floatingip_agent_gateway_port(
        self, context, host_id, ext_net_id):
        """Function to delete FIP gateway port with given ext_net_id."""
        # delete any fip agent gw port
        device_filter = {'device_owner': [const.DEVICE_OWNER_AGENT_GW],
                         'network_id': [ext_net_id]}
        ports = self._core_plugin.get_ports(context,
                                            filters=device_filter)
        for p in ports:
            if not host_id or p[portbindings.HOST_ID] == host_id:
                self._core_plugin.ipam.delete_port(context, p['id'])
                if host_id:
                    return

    def check_for_fip_and_create_agent_gw_port_on_host_if_not_exists(
            self, context, port, host):
        """Create fip agent_gw_port on host if not exists"""
        fip = self._get_floatingip_on_port(context, port_id=port['id'])
        if not fip:
            return
        network_id = fip.get('floating_network_id')
        agent_gw_port = self.create_fip_agent_gw_port_if_not_exists(
            context.elevated(), network_id, host)
        LOG.debug("Port-in-Migration: Floatingip Agent Gateway port "
                  "%(gw)s created for the future host: %(dest_host)s",
                  {'gw': agent_gw_port,
                   'dest_host': host})

    def create_fip_agent_gw_port_if_not_exists(
        self, context, network_id, host):
        """Function to return the FIP Agent GW port.

        This function will create a FIP Agent GW port
        if required. If the port already exists, it
        will return the existing port and will not
        create a new one.
        """
        l3_agent_db = self._get_agent_by_type_and_host(
            context, const.AGENT_TYPE_L3, host)
        if l3_agent_db:
            LOG.debug("Agent ID exists: %s", l3_agent_db['id'])
            f_port = self._get_agent_gw_ports_exist_for_network(
                context, network_id, host, l3_agent_db['id'])
            if not f_port:
                LOG.info(_LI('Agent Gateway port does not exist,'
                             ' so create one: %s'), f_port)
                port_data = {'tenant_id': '',
                             'network_id': network_id,
                             'device_id': l3_agent_db['id'],
                             'device_owner': const.DEVICE_OWNER_AGENT_GW,
                             portbindings.HOST_ID: host,
                             'admin_state_up': True,
                             'name': ''}
                agent_port = p_utils.create_port(self._core_plugin, context,
                                                 {'port': port_data})
                if agent_port:
                    self._populate_mtu_and_subnets_for_ports(context,
                                                             [agent_port])
                    return agent_port
                msg = _("Unable to create the Agent Gateway Port")
                raise n_exc.BadRequest(resource='router', msg=msg)
            else:
                self._populate_mtu_and_subnets_for_ports(context, [f_port])
                return f_port

    def _get_snat_interface_ports_for_router(self, context, router_id):
        """Return all existing snat_router_interface ports."""
        qry = context.session.query(l3_models.RouterPort)
        qry = qry.filter_by(
            router_id=router_id,
            port_type=const.DEVICE_OWNER_ROUTER_SNAT
        )

        ports = [self._core_plugin._make_port_dict(rp.port)
                 for rp in qry]
        return ports

    def _add_csnat_router_interface_port(
            self, context, router, network_id, subnet_id, do_pop=True):
        """Add SNAT interface to the specified router and subnet."""
        port_data = {'tenant_id': '',
                     'network_id': network_id,
                     'fixed_ips': [{'subnet_id': subnet_id}],
                     'device_id': router.id,
                     'device_owner': const.DEVICE_OWNER_ROUTER_SNAT,
                     'admin_state_up': True,
                     'name': ''}
        snat_port = p_utils.create_port(self._core_plugin, context,
                                        {'port': port_data})
        if not snat_port:
            msg = _("Unable to create the SNAT Interface Port")
            raise n_exc.BadRequest(resource='router', msg=msg)

        with context.session.begin(subtransactions=True):
            router_port = l3_models.RouterPort(
                port_id=snat_port['id'],
                router_id=router.id,
                port_type=const.DEVICE_OWNER_ROUTER_SNAT
            )
            context.session.add(router_port)

        if do_pop:
            return self._populate_mtu_and_subnets_for_ports(context,
                                                            [snat_port])
        return snat_port

    def _create_snat_intf_ports_if_not_exists(self, context, router):
        """Function to return the snat interface port list.

        This function will return the snat interface port list
        if it exists. If the port does not exist it will create
        new ports and then return the list.
        """
        port_list = self._get_snat_interface_ports_for_router(
            context, router.id)
        if port_list:
            self._populate_mtu_and_subnets_for_ports(context, port_list)
            return port_list
        port_list = []

        int_ports = (
            rp.port for rp in
            router.attached_ports.filter_by(
                port_type=const.DEVICE_OWNER_DVR_INTERFACE
            )
        )
        LOG.info(_LI('SNAT interface port list does not exist,'
                     ' so create one: %s'), port_list)
        for intf in int_ports:
            if intf.fixed_ips:
                # Passing the subnet for the port to make sure the IP's
                # are assigned on the right subnet if multiple subnet
                # exists
                snat_port = self._add_csnat_router_interface_port(
                    context, router, intf['network_id'],
                    intf['fixed_ips'][0]['subnet_id'], do_pop=False)
                port_list.append(snat_port)
        if port_list:
            self._populate_mtu_and_subnets_for_ports(context, port_list)
        return port_list

    def _generate_arp_table_and_notify_agent(
        self, context, fixed_ip, mac_address, notifier):
        """Generates the arp table entry and notifies the l3 agent."""
        ip_address = fixed_ip['ip_address']
        subnet = fixed_ip['subnet_id']
        arp_table = {'ip_address': ip_address,
                     'mac_address': mac_address,
                     'subnet_id': subnet}
        filters = {'fixed_ips': {'subnet_id': [subnet]},
                   'device_owner': [const.DEVICE_OWNER_DVR_INTERFACE]}
        ports = self._core_plugin.get_ports(context, filters=filters)
        routers = [port['device_id'] for port in ports]
        for router_id in routers:
            notifier(context, router_id, arp_table)

    def _should_update_arp_entry_for_dvr_service_port(self, port_dict):
        # Check this is a valid VM or service port
        return (n_utils.is_dvr_serviced(port_dict['device_owner']) and
                port_dict['fixed_ips'])

    def _get_subnet_id_for_given_fixed_ip(
        self, context, fixed_ip, port_dict):
        """Returns the subnet_id that matches the fixedip on a network."""
        filters = {'network_id': [port_dict['network_id']]}
        subnets = self._core_plugin.get_subnets(context, filters)
        for subnet in subnets:
            if ipam_utils.check_subnet_ip(subnet['cidr'], fixed_ip):
                return subnet['id']

    def _get_allowed_address_pair_fixed_ips(self, context, port_dict):
        """Returns all fixed_ips associated with the allowed_address_pair."""
        aa_pair_fixed_ips = []
        if port_dict.get('allowed_address_pairs'):
            for address_pair in port_dict['allowed_address_pairs']:
                aap_ip_cidr = address_pair['ip_address'].split("/")
                if len(aap_ip_cidr) == 1 or int(aap_ip_cidr[1]) == 32:
                    subnet_id = self._get_subnet_id_for_given_fixed_ip(
                        context, aap_ip_cidr[0], port_dict)
                    if subnet_id is not None:
                        fixed_ip = {'subnet_id': subnet_id,
                                    'ip_address': aap_ip_cidr[0]}
                        aa_pair_fixed_ips.append(fixed_ip)
                    else:
                        LOG.debug("Subnet does not match for the given "
                                  "fixed_ip %s for arp update", aap_ip_cidr[0])
        return aa_pair_fixed_ips

    def update_arp_entry_for_dvr_service_port(self, context, port_dict):
        """Notify L3 agents of ARP table entry for dvr service port.

        When a dvr service port goes up, look for the DVR router on
        the port's subnet, and send the ARP details to all
        L3 agents hosting the router to add it.
        If there are any allowed_address_pairs associated with the port
        those fixed_ips should also be updated in the ARP table.
        """
        if not self._should_update_arp_entry_for_dvr_service_port(port_dict):
            return
        fixed_ips = port_dict['fixed_ips']
        allowed_address_pair_fixed_ips = (
            self._get_allowed_address_pair_fixed_ips(context, port_dict))
        changed_fixed_ips = fixed_ips + allowed_address_pair_fixed_ips
        for fixed_ip in changed_fixed_ips:
            self._generate_arp_table_and_notify_agent(
                context, fixed_ip, port_dict['mac_address'],
                self.l3_rpc_notifier.add_arp_entry)

    def delete_arp_entry_for_dvr_service_port(
        self, context, port_dict, fixed_ips_to_delete=None):
        """Notify L3 agents of ARP table entry for dvr service port.

        When a dvr service port goes down, look for the DVR
        router on the port's subnet, and send the ARP details to all
        L3 agents hosting the router to delete it.
        If there are any allowed_address_pairs associated with the
        port, those fixed_ips should be removed from the ARP table.
        """
        if not self._should_update_arp_entry_for_dvr_service_port(port_dict):
            return
        if not fixed_ips_to_delete:
            fixed_ips = port_dict['fixed_ips']
            allowed_address_pair_fixed_ips = (
                self._get_allowed_address_pair_fixed_ips(context, port_dict))
            fixed_ips_to_delete = fixed_ips + allowed_address_pair_fixed_ips
        for fixed_ip in fixed_ips_to_delete:
            self._generate_arp_table_and_notify_agent(
                context, fixed_ip, port_dict['mac_address'],
                self.l3_rpc_notifier.del_arp_entry)

    def delete_csnat_router_interface_ports(self, context,
                                            router, subnet_id=None):
        # Each csnat router interface port is associated
        # with a subnet, so we need to pass the subnet id to
        # delete the right ports.

        # TODO(markmcclain): This is suboptimal but was left to reduce
        # changeset size since it is late in cycle
        ports = [
            rp.port.id for rp in
            router.attached_ports.filter_by(
                    port_type=const.DEVICE_OWNER_ROUTER_SNAT)
            if rp.port
        ]

        c_snat_ports = self._core_plugin.get_ports(
            context,
            filters={'id': ports}
        )
        for p in c_snat_ports:
            if subnet_id is None or not p['fixed_ips']:
                if not p['fixed_ips']:
                    LOG.info(_LI("CSNAT port has no IPs: %s"), p)
                self._core_plugin.delete_port(context,
                                              p['id'],
                                              l3_port_check=False)
            else:
                if p['fixed_ips'][0]['subnet_id'] == subnet_id:
                    LOG.debug("Subnet matches: %s", subnet_id)
                    self._core_plugin.delete_port(context,
                                                  p['id'],
                                                  l3_port_check=False)

    @db_api.retry_if_session_inactive()
    def create_floatingip(self, context, floatingip,
                          initial_status=const.FLOATINGIP_STATUS_ACTIVE):
        floating_ip = self._create_floatingip(
            context, floatingip, initial_status)
        self._notify_floating_ip_change(context, floating_ip)
        return floating_ip

    def _notify_floating_ip_change(self, context, floating_ip):
        router_id = floating_ip['router_id']
        fixed_port_id = floating_ip['port_id']
        # we need to notify agents only in case Floating IP is associated
        if not router_id or not fixed_port_id:
            return

        try:
            # using admin context as router may belong to admin tenant
            router = self._get_router(context.elevated(), router_id)
        except l3.RouterNotFound:
            LOG.warning(_LW("Router %s was not found. "
                            "Skipping agent notification."),
                        router_id)
            return

        if is_distributed_router(router):
            host = self._get_dvr_service_port_hostid(context, fixed_port_id)
            dest_host = self._get_dvr_migrating_service_port_hostid(
                context, fixed_port_id)
            self.l3_rpc_notifier.routers_updated_on_host(
                context, [router_id], host)
            if dest_host and dest_host != host:
                self.l3_rpc_notifier.routers_updated_on_host(
                    context, [router_id], dest_host)
        else:
            self.notify_router_updated(context, router_id)

    @db_api.retry_if_session_inactive()
    def update_floatingip(self, context, id, floatingip):
        old_floatingip, floatingip = self._update_floatingip(
            context, id, floatingip)
        self._notify_floating_ip_change(context, old_floatingip)
        if (floatingip['router_id'] != old_floatingip['router_id'] or
                floatingip['port_id'] != old_floatingip['port_id']):
            self._notify_floating_ip_change(context, floatingip)
        return floatingip

    @db_api.retry_if_session_inactive()
    def delete_floatingip(self, context, id):
        floating_ip = self._delete_floatingip(context, id)
        self._notify_floating_ip_change(context, floating_ip)

    def _get_address_pair_active_port_with_fip(
            self, context, port_dict, port_addr_pair_ip):
        port_valid_state = (port_dict['admin_state_up'] or
            (port_dict['status'] == const.PORT_STATUS_ACTIVE))
        if not port_valid_state:
            return
        query = context.session.query(l3_models.FloatingIP).filter(
            l3_models.FloatingIP.fixed_ip_address == port_addr_pair_ip)
        fip = query.first()
        return self._core_plugin.get_port(
            context, fip.fixed_port_id) if fip else None

    def update_unbound_allowed_address_pair_port_binding(
            self, context, service_port_dict,
            port_address_pairs, address_pair_port=None):
        """Update allowed address pair port with host and device_owner

        This function sets the host and device_owner to the port
        associated with the port_addr_pair_ip with the port_dict's
        host and device_owner.
        """
        port_addr_pair_ip = port_address_pairs['ip_address']
        if not address_pair_port:
            address_pair_port = self._get_address_pair_active_port_with_fip(
                context, service_port_dict, port_addr_pair_ip)
        if address_pair_port:
            host = service_port_dict[portbindings.HOST_ID]
            dev_owner = service_port_dict['device_owner']
            address_pair_dev_owner = address_pair_port.get('device_owner')
            # If the allowed_address_pair port already has an associated
            # device owner, and if the device_owner is a dvr serviceable
            # port, then don't update the device_owner.
            port_profile = address_pair_port.get(portbindings.PROFILE, {})
            if n_utils.is_dvr_serviced(address_pair_dev_owner):
                port_profile['original_owner'] = address_pair_dev_owner
                port_data = {portbindings.HOST_ID: host,
                             portbindings.PROFILE: port_profile}
            else:
                port_data = {portbindings.HOST_ID: host,
                             'device_owner': dev_owner}
            update_port = self._core_plugin.update_port(
                context, address_pair_port['id'], {'port': port_data})
            return update_port

    def remove_unbound_allowed_address_pair_port_binding(
            self, context, service_port_dict,
            port_address_pairs, address_pair_port=None):
        """Remove allowed address pair port binding and device_owner

        This function clears the host and device_owner associated with
        the port_addr_pair_ip.
        """
        port_addr_pair_ip = port_address_pairs['ip_address']
        if not address_pair_port:
            address_pair_port = self._get_address_pair_active_port_with_fip(
                context, service_port_dict, port_addr_pair_ip)
        if address_pair_port:
            # Before reverting the changes, fetch the original
            # device owner saved in profile and update the port
            port_profile = address_pair_port.get(portbindings.PROFILE)
            orig_device_owner = ""
            if port_profile:
                orig_device_owner = port_profile.get('original_owner')
                del port_profile['original_owner']
            port_data = {portbindings.HOST_ID: "",
                         'device_owner': orig_device_owner,
                         portbindings.PROFILE: port_profile}
            update_port = self._core_plugin.update_port(
                context, address_pair_port['id'], {'port': port_data})
            return update_port


def is_distributed_router(router):
    """Return True if router to be handled is distributed."""
    try:
        # See if router is a DB object first
        requested_router_type = router.extra_attributes.distributed
    except AttributeError:
        # if not, try to see if it is a request body
        requested_router_type = router.get('distributed')
    if validators.is_attr_set(requested_router_type):
        return requested_router_type
    return cfg.CONF.router_distributed
