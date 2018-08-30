""" opentrons.system.nmcli: Functions and data for interacting with nmcli

The functions contained here are for bridging Python calls with nmcli command
line invocations. They are in general not safe to call anywhere except an
Opentrons robot; on systems that do not have network-manager (like OSX, Windows
and some Linux distributions) they will not work, but on systems that _do_ they
may alter or destroy networking configurations.

In general, the functions here are light shims around nmcli invocations. This
is relevant in particular because they mostly do not handle exceptions coming
from subprocess itself, only parsing nmcli output.
"""

import logging
import re
from shlex import quote
from asyncio import subprocess as as_subprocess

log = logging.getLogger(__name__)

SUPPORTED_SECURITY_TYPES = ('none', 'wpa-psk')


async def available_ssids():
    """ List the visible (broadcasting SSID) wireless networks.

    Returns a list of the SSIDs. They may contain spaces and should be escaped
    if later passed to a shell.
    """
    fields = ['ssid', 'signal', 'active']
    cmd = ['--terse',
           '--fields',
           ','.join(fields),
           'device',
           'wifi',
           'list']
    out, _ = await _call(cmd)
    return _dict_from_terse_tabular(
        fields, out,
        transformers={'signal': lambda s: int(s) if s.isdigit() else None,
                      'active': lambda s: s.lower() == 'yes'})


async def is_connected():
    """ Return nmcli's connection measure: none/portal/limited/full/unknown"""
    res, _ = await _call(['networking', 'connectivity'])
    return res


async def connections():
    """ Return the list of configured connections.

    This is all connections that nmcli knows about and manages.
    Each connection is a dict containing some basic information - the
    information retrievable from nmcli connection show. Further information
    should be queried on a connection by connection basis.
    """
    fields = ['name', 'type', 'active']
    res, _ = await _call(['-t', '-f', ','.join(fields), 'connection', 'show'])
    return _dict_from_terse_tabular(
        fields,
        res,
        # ’ethernet’ or ’wireless’ from ’802-11-wireless’ or ’802-4-ethernet’
        # and bools from ’yes’ or ’no
        transformers={'type': lambda s: s.split('-')[-1],
                      'active': lambda s: s.lower() == 'yes'}
    )


async def connection_exists(ssid):
    """ If there is already a connection for this ssid, return the name of
    the connection; if there is not, return None.
    """
    nmcli_conns = await connections()
    for wifi in [c['name']
                 for c in nmcli_conns if c['type'] == 'wireless']:
        res, _ = await _call(['-t', '-f', '802-11-wireless.ssid',
                              '-m', 'tabular',
                              'connection', 'show', wifi])
        if res == ssid:
            return wifi
    return None


async def configure(ssid,
                    security_type=None,
                    psk=None,
                    hidden=False):
    """ Configure a connection but do not bring it up (though it is configured
    for autoconnect).

    Returns (success, message) where ``success`` is a ``bool`` and ``message``
    is a ``str``.

    Only anticipated failures are treated that way - for instance, an ssid
    that doesn't exist will get a False and a message; a system where nmcli
    is not found will raise a CalledProcessError.
    """
    if None is security_type and None is not psk:
        security_type = 'wpa-psk'
    if security_type and security_type not in SUPPORTED_SECURITY_TYPES:
        message = 'Only security types {} are supported'\
            .format(SUPPORTED_SECURITY_TYPES)
        log.error("Specified security type <{}> is not supported"
                  .format(security_type))
        return False, message

    already = await connection_exists(ssid)
    if already:
        # TODO(seth, 8/29/2018): We may need to do connection modifies
        # here for EAP configuration if e.g. we’re passing a keyfile in a
        # different http request
        _1, _2 = await _call(['connection', 'delete', already])
    configure_cmd = ['connection', 'add',
                     'save', 'yes',
                     'autoconnect', 'yes',
                     'ifname', 'wlan0',
                     'type', 'wifi',
                     'con-name', ssid,
                     'wifi.ssid', ssid]
    if security_type:
        configure_cmd += ['wifi-sec.key-mgmt', security_type]
    if psk:
        configure_cmd += ['wifi-sec.psk', psk]
    if hidden:
        configure_cmd += ['hidden', 'true']
    res, err = await _call(configure_cmd)
    # nmcli connection add returns a string that looks like
    # "connection-name (connection-uuid) successfully added.". This
    # unfortunately doesn’t respect the --terse flag, so we need to
    # regex out the name or the uuid to use later in connection up; the
    # uuid is slightly more regular, so that’s what we use.
    uuid_matches = re.search('\(([\w\d-]+)\) successfully', res) # noqa
    if not uuid_matches:
        return False, err.split('\r')[-1]
    uuid = uuid_matches.group(1)
    res, err = await _call(['connection', 'up', 'uuid', uuid])
    if 'Connection successfully activated' not in res:
        return False, err.split('\r')[-1]
    else:
        return True, res


async def remove(ssid=None, name=None) -> (bool, str):
    """ Remove a network. Depending on what is known, specify either ssid
    (in which case this function will call ``connection_exists`` to get the
    nmcli connection name) or the nmcli connection name directly.

    Returns (True, msg) if the connection was deleted, (False, msg) otherwise.
    """
    if None is not ssid:
        name = await connection_exists(ssid)
    if None is not name:
        res, err = await _call(['connection', 'delete', name])
        if 'successfully deleted' in res:
            return True, res
        else:
            return False, err
    else:
        return False, 'No connection for ssid {}'.format(ssid)


async def _call(cmd) -> (str, str):
    """
    Runs the command in a subprocess and returns the captured stdout output.
    :param cmd: a list of arguments to nmcli. Should not include nmcli itself.

    :return: (stdout, stderr)
    """
    to_exec = [quote(c) for c in ['nmcli'] + cmd]
    cmd_str = ' '.join(to_exec)
    # We have to use a shell invocation here because nmcli will not accept
    # secrets specified on the command line unless it’s in a shell. The other
    # option is editing the connection configuration file in /etc/ afterwards
    # (or using d-bus and pretending to be an auth agent)
    proc = await as_subprocess.create_subprocess_shell(
        cmd_str,
        stdout=as_subprocess.PIPE, stderr=as_subprocess.PIPE)
    out, err = await proc.communicate()
    out_str, err_str = out.decode().strip(), err.decode().strip()
    sanitized = sanitize_args(to_exec)
    log.debug('{}: stdout={}'.format(' '.join(sanitized), out_str))
    if err_str:
        log.info('{}: stderr={}'.format(' '.join(sanitized), err_str))
    return out_str, err_str


def sanitize_args(cmd) -> (str, str):
    """ Filter the command so that it no longer contains passwords
    """
    sanitized = []
    for idx, fieldname in enumerate(cmd):
        if idx > 0 and 'wifi-sec.psk' in cmd[idx-1]:
            sanitized.append('****')
        else:
            sanitized.append(fieldname)
    return sanitized


def _dict_from_terse_tabular(names, inp, transformers={}):
    """ Parse NMCLI terse tabular output into a list of Python dict.

    ``names`` is a list of strings of field names to apply to the input data,
    which is assumed to be colon separated.

    ``inp`` is the input as a string (i.e. already decode()d) from nmcli

    ``transformers`` is a dict mapping field names to callables of the form
    f: str -> any. If a fieldname is in transformers, that callable will be
    invoked on the field matching the name and the result stored.

    The return value is a list with one element per valid line of input, where
    each element is a dict with keys taken from names and values from the input
    """
    res = []
    for n in names:
        if n not in transformers:
            transformers[n] = lambda s: s
    for line in inp.split('\n'):
        if len(line) < 3:
            continue
        fields = line.split(':')
        res.append(dict([
            (elem[0], transformers[elem[0]](elem[1]))
            for elem in zip(names, fields)]))
    return res
