#!/usr/bin/env bash

set -ex

VENV=${1:-"dsvm-functional"}
FLAVOR=${2:-"all"}

GATE_DEST=$BASE/new
NEUTRON_PATH=$GATE_DEST/neutron
GATE_HOOKS=$NEUTRON_PATH/neutron/tests/contrib/hooks
DEVSTACK_PATH=$GATE_DEST/devstack
LOCAL_CONF=$DEVSTACK_PATH/late-local.conf
RALLY_EXTRA_DIR=$NEUTRON_PATH/rally-jobs/extra
DSCONF=/tmp/devstack-tools/bin/dsconf

# Install devstack-tools used to produce local.conf; we can't rely on
# test-requirements.txt because the gate hook is triggered before neutron is
# installed
sudo -H pip install virtualenv
virtualenv /tmp/devstack-tools
/tmp/devstack-tools/bin/pip install -U devstack-tools==0.4.0

# Inject config from hook into localrc
function load_rc_hook {
    local hook="$1"
    local tmpfile
    local config
    tmpfile=$(tempfile)
    config=$(cat $GATE_HOOKS/$hook)
    echo "[[local|localrc]]" > $tmpfile
    $DSCONF setlc_raw $tmpfile "$config"
    $DSCONF merge_lc $LOCAL_CONF $tmpfile
    rm -f $tmpfile
}


# Inject config from hook into local.conf
function load_conf_hook {
    local hook="$1"
    $DSCONF merge_lc $LOCAL_CONF $GATE_HOOKS/$hook
}


# Tweak gate configuration for our rally scenarios
function load_rc_for_rally {
    for file in $(ls $RALLY_EXTRA_DIR/*.setup); do
        tmpfile=$(tempfile)
        config=$(cat $file)
        echo "[[local|localrc]]" > $tmpfile
        $DSCONF setlc_raw $tmpfile "$config"
        $DSCONF merge_lc $LOCAL_CONF $tmpfile
        rm -f $tmpfile
    done
}


case $VENV in
"dsvm-functional"|"dsvm-fullstack")
    # The following need to be set before sourcing
    # configure_for_func_testing.
    GATE_STACK_USER=stack
    PROJECT_NAME=neutron
    IS_GATE=True
    LOCAL_CONF=$DEVSTACK_PATH/local.conf

    source $DEVSTACK_PATH/functions
    source $NEUTRON_PATH/devstack/lib/ovs

    source $NEUTRON_PATH/tools/configure_for_func_testing.sh

    configure_host_for_func_testing

    # Because of bug present in current Ubuntu Xenial kernel version
    # we need a fix for VXLAN local tunneling.
    if [[ "$VENV" =~ "dsvm-fullstack" ]]; then
        # The OVS_BRANCH variable is used by git checkout. In the case below,
        # we use openvswitch commit 175be4bf23a206b264719b5661707af186b31f32
        # that contains a fix for usage of VXLAN tunnels on a single node
        # (commit 741f47cf35df2bfc7811b2cff75c9bb8d05fd26f) and is compatible
        # with kernel 4.4.0-145
        # NOTE(slaweq): Replace with a release tag when one is available.
        # See commit 138df3e563de9da0e5a4155b3534a69621495742 (on the ovs repo).
        OVS_BRANCH="175be4bf23a206b264719b5661707af186b31f32"
        compile_ovs_kernel_module
    elif [[ "$VENV" =~ "dsvm-functional" ]]; then
        # NOTE(slaweq): there is some bug in keepalived
        # 1:1.2.24-1ubuntu0.16.04.1, and because of that we have to use older
        # version for tests as workaround. For details check
        # https://bugs.launchpad.net/neutron/+bug/1788185
        # https://bugs.launchpad.net/ubuntu/+source/keepalived/+bug/1789045
        sudo apt-get install -y --allow-downgrades keepalived=1:1.2.19-1
    fi

    load_conf_hook iptables_verify
    # Make the workspace owned by the stack user
    sudo chown -R $STACK_USER:$STACK_USER $BASE
    ;;

# TODO(ihrachys): remove dsvm-scenario from the list when it's no longer used in project-config
"api"|"api-pecan"|"full-ovsfw"|"full-pecan"|"dsvm-scenario"|"dsvm-scenario-ovs"|"dsvm-scenario-linuxbridge")
    load_rc_hook api_${FLAVOR}_extensions
    load_conf_hook quotas
    load_rc_hook dns
    load_rc_hook qos
    load_rc_hook trunk
    load_conf_hook mtu
    load_conf_hook osprofiler
    if [[ "$VENV" =~ "dsvm-scenario" ]]; then
        load_conf_hook iptables_verify
        load_rc_hook ubuntu_image
    fi
    if [[ "$VENV" =~ "pecan" ]]; then
        load_conf_hook pecan
    fi
    if [[ "$VENV" =~ "ovs" ]]; then
        load_conf_hook ovsfw
    fi

    export DEVSTACK_LOCALCONF=$(cat $LOCAL_CONF)
    $BASE/new/devstack-gate/devstack-vm-gate.sh
    ;;

"rally")
    load_rc_for_rally
    export DEVSTACK_LOCALCONF=$(cat $LOCAL_CONF)
    $BASE/new/devstack-gate/devstack-vm-gate.sh
    ;;

*)
    echo "Unrecognized environment $VENV".
    exit 1
esac
