#
# Copyright 2017-2018 Ettus Research, a National Instruments Company
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
Magnesium dboard implementation module
"""

from __future__ import print_function
import os
import threading
from six import iterkeys, iteritems
from usrp_mpm import lib # Pulls in everything from C++-land
from usrp_mpm.dboard_manager import DboardManagerBase
from usrp_mpm.dboard_manager.mg_periphs import TCA6408, MgCPLD
from usrp_mpm.dboard_manager.mg_init import MagnesiumInitManager
from usrp_mpm.mpmlog import get_logger
from usrp_mpm.sys_utils.uio import open_uio
from usrp_mpm.sys_utils.udev import get_eeprom_paths
from usrp_mpm.bfrfs import BufferFS

###############################################################################
# SPI Helpers
###############################################################################
def create_spidev_iface_lmk(dev_node):
    """
    Create a regs iface from a spidev node
    """
    return lib.spi.make_spidev_regs_iface(
        str(dev_node),
        1000000, # Speed (Hz)
        3, # SPI mode
        8, # Addr shift
        0, # Data shift
        1<<23, # Read flag
        0, # Write flag
    )

def create_spidev_iface_cpld(dev_node):
    """
    Create a regs iface from a spidev node
    """
    return lib.spi.make_spidev_regs_iface(
        str(dev_node),
        1000000, # Speed (Hz)
        0, # SPI mode
        16, # Addr shift
        0, # Data shift
        1<<23, # Read flag
        0, # Write flag
    )

def create_spidev_iface_phasedac(dev_node):
    """
    Create a regs iface from a spidev node (ADS5681)
    """
    return lib.spi.make_spidev_regs_iface(
        str(dev_node),
        1000000, # Speed (Hz)
        1, # SPI mode
        16, # Addr shift
        0, # Data shift
        0, # Read flag (phase DAC is write-only)
        0, # Write flag
    )

###############################################################################
# Main dboard control class
###############################################################################
class Magnesium(DboardManagerBase):
    """
    Holds all dboard specific information and methods of the magnesium dboard
    """
    #########################################################################
    # Overridables
    #
    # See DboardManagerBase for documentation on these fields
    #########################################################################
    pids = [0x150]
    rx_sensor_callback_map = {
        'lowband_lo_locked': 'get_lowband_tx_lo_locked_sensor',
        'ad9371_lo_locked': 'get_ad9371_tx_lo_locked_sensor',
    }
    tx_sensor_callback_map = {
        'lowband_lo_locked': 'get_lowband_rx_lo_locked_sensor',
        'ad9371_lo_locked': 'get_ad9371_rx_lo_locked_sensor',
    }
    # Maps the chipselects to the corresponding devices:
    spi_chipselect = {"cpld": 0, "lmk": 1, "mykonos": 2, "phase_dac": 3}
    ### End of overridables #################################################
    # Class-specific, but constant settings:
    spi_factories = {
        "cpld": create_spidev_iface_cpld,
        "lmk": create_spidev_iface_lmk,
        "phase_dac": create_spidev_iface_phasedac,
    }
    #file system path to i2c-adapter/mux
    base_i2c_adapter = '/sys/class/i2c-adapter'
    # Map I2C channel to slot index
    i2c_chan_map = {0: 'i2c-9', 1: 'i2c-10'}
    # This map describes how the user data is stored in EEPROM. If a dboard rev
    # changes the way the EEPROM is used, we add a new entry. If a dboard rev
    # is not found in the map, then we go backward until we find a suitable rev
    user_eeprom = {
        2: { # RevC
            'label': "e0004000.i2c",
            'offset': 1024,
            'max_size': 32786 - 1024,
            'alignment': 1024,
        },
    }
    default_master_clock_rate = 125e6
    default_time_source = 'internal'
    default_current_jesd_rate = 2500e6

    def __init__(self, slot_idx, **kwargs):
        super(Magnesium, self).__init__(slot_idx, **kwargs)
        self.log = get_logger("Magnesium-{}".format(slot_idx))
        self.log.trace("Initializing Magnesium daughterboard, slot index %d",
                       self.slot_idx)
        self.rev = int(self.device_info['rev'])
        self.log.trace("This is a rev: {}".format(chr(65 + self.rev)))
        # This is a default ref clock freq, it must be updated before init() is
        # called!
        self.ref_clock_freq = None
        # These will get updated during init()
        self.master_clock_rate = None
        self.current_jesd_rate = None
        # Predeclare some attributes to make linter happy:
        self.lmk = None
        self._port_expander = None
        self.mykonos = None
        self.eeprom_fs = None
        self.eeprom_path = None
        self.cpld = None
        self._init_args = {}
        # Now initialize all peripherals. If that doesn't work, put this class
        # into a non-functional state (but don't crash, or we can't talk to it
        # any more):
        try:
            self._init_periphs()
            self._periphs_initialized = True
        except Exception as ex:
            self.log.error("Failed to initialize peripherals: %s",
                           str(ex))
            self._periphs_initialized = False

    def _init_periphs(self):
        """
        Initialize power and peripherals that don't need user-settings
        """
        self._port_expander = TCA6408(self._get_i2c_dev(self.slot_idx))
        self._power_on()
        self.log.debug("Loading C++ drivers...")

        # The Mykonos TX DeFramer lane crossbar requires configuration on a per-slot
        # basis due to motherboard MGT lane swapping.
        # The RX framer lane crossbar configuration
        # is identical for both slots and is hard-coded within the Mykonos API.
        deserializer_lane_xbar = 0xD2 if self.slot_idx == 0 else 0x72

        self._device = lib.dboards.magnesium_manager(
            self._spi_nodes['mykonos'],
            deserializer_lane_xbar
        )
        self.mykonos = self._device.get_radio_ctrl()
        self.spi_lock = self._device.get_spi_lock()
        self.log.trace("Loaded C++ drivers.")
        self._init_myk_api(self.mykonos)
        self.log.debug(
            "AD9371: ARM version: {arm_ver} API version: {api_ver} "
            "Device revision: {dev_rev}".format(
                arm_ver=self.get_arm_version(),
                api_ver=self.get_api_version(),
                dev_rev=self.get_device_rev(),
            )
        )
        self.eeprom_fs, self.eeprom_path = self._init_user_eeprom(
            self._get_user_eeprom_info(self.rev)
        )
        self.log.trace("Loading SPI devices...")
        self._spi_ifaces = {
            key: self.spi_factories[key](self._spi_nodes[key])
            for key in self.spi_factories
        }
        self.cpld = MgCPLD(self._spi_ifaces['cpld'], self.log)
        self.device_info['cpld_rev'] = \
                str(self.cpld.major_rev) + '.' + str(self.cpld.minor_rev)

    def _power_on(self):
        " Turn on power to daughterboard "
        self.log.trace("Powering on slot_idx={}...".format(self.slot_idx))
        self._port_expander.set("PWR-EN-3.6V")
        self._port_expander.set("PWR-EN-1.5V")
        self._port_expander.set("PWR-EN-5.5V")
        self._port_expander.set("LED")

    def _power_off(self):
        " Turn off power to daughterboard "
        self.log.trace("Powering off slot_idx={}...".format(self.slot_idx))
        self._port_expander.reset("PWR-EN-3.6V")
        self._port_expander.reset("PWR-EN-1.5V")
        self._port_expander.reset("PWR-EN-5.5V")
        self._port_expander.reset("LED")

    def _get_i2c_dev(self, slot_idx):
        " Return the I2C path for this daughterboard "
        import pyudev
        context = pyudev.Context()
        i2c_dev_path = os.path.join(
            self.base_i2c_adapter,
            self.i2c_chan_map[slot_idx]
        )
        return pyudev.Devices.from_sys_path(context, i2c_dev_path)

    def _init_myk_api(self, myk):
        """
        Propagate the C++ Mykonos API into Python land.
        """
        def export_method(obj, method):
            " Export a method object, including docstring "
            meth_obj = getattr(obj, method)
            def func(*args):
                " Functor for storing docstring too "
                return meth_obj(*args)
            func.__doc__ = meth_obj.__doc__
            return func
        self.log.trace("Forwarding AD9371 methods to Magnesium class...")
        for method in [
                x for x in dir(self.mykonos)
                if not x.startswith("_") and \
                        callable(getattr(self.mykonos, x))]:
            self.log.trace("adding {}".format(method))
            setattr(self, method, export_method(myk, method))

    def _get_user_eeprom_info(self, rev):
        """
        Return an EEPROM access map (from self.user_eeprom) based on the rev.
        """
        rev_for_lookup = rev
        while rev_for_lookup not in self.user_eeprom:
            if rev_for_lookup < 0:
                raise RuntimeError("Could not find a user EEPROM map for "
                                   "revision %d!", rev)
            rev_for_lookup -= 1
        assert rev_for_lookup in self.user_eeprom, \
                "Invalid EEPROM lookup rev!"
        return self.user_eeprom[rev_for_lookup]

    def _init_user_eeprom(self, eeprom_info):
        """
        Reads out user-data EEPROM, and intializes a BufferFS object from that.
        """
        self.log.trace("Initializing EEPROM user data...")
        eeprom_paths = get_eeprom_paths(eeprom_info.get('label'))
        self.log.trace("Found the following EEPROM paths: `{}'".format(
            eeprom_paths))
        eeprom_path = eeprom_paths[self.slot_idx]
        self.log.trace("Selected EEPROM path: `{}'".format(eeprom_path))
        user_eeprom_offset = eeprom_info.get('offset', 0)
        self.log.trace("Selected EEPROM offset: %d", user_eeprom_offset)
        user_eeprom_data = open(eeprom_path, 'rb').read()[user_eeprom_offset:]
        self.log.trace("Total EEPROM size is: %d bytes", len(user_eeprom_data))
        # FIXME verify EEPROM sectors
        return BufferFS(
            user_eeprom_data,
            max_size=eeprom_info.get('max_size'),
            alignment=eeprom_info.get('alignment', 1024),
            log=self.log
        ), eeprom_path


    def init(self, args):
        """
        Execute necessary init dance to bring up dboard
        """
        # Sanity checks and input validation:
        self.log.debug("init() called with args `{}'".format(
            ",".join(['{}={}'.format(x, args[x]) for x in args])
        ))
        if not self._periphs_initialized:
            error_msg = "Cannot run init(), peripherals are not initialized!"
            self.log.error(error_msg)
            raise RuntimeError(error_msg)
        # Check if ref clock freq changed (would require a full init)
        ref_clk_freq_changed = False
        if 'ref_clk_freq' in args:
            new_ref_clock_freq = float(args['ref_clk_freq'])
            assert new_ref_clock_freq in (10e6, 20e6, 25e6)
            if new_ref_clock_freq != self.ref_clock_freq:
                self.ref_clock_freq = float(args['ref_clk_freq'])
                ref_clk_freq_changed = True
        assert self.ref_clock_freq is not None
        # Check if master clock freq changed (would require a full init)
        master_clock_rate = \
            float(args.get('master_clock_rate',
                           self.default_master_clock_rate))
        assert master_clock_rate in (122.88e6, 125e6, 153.6e6), \
                "Invalid master clock rate: {:.02f} MHz".format(
                    master_clock_rate / 1e6)
        master_clock_rate_changed = \
            master_clock_rate != self.master_clock_rate
        if master_clock_rate_changed:
            self.master_clock_rate = master_clock_rate
            self.log.debug(
                "Updating master clock rate to {:.02f} MHz!"
                .format(self.master_clock_rate / 1e6)
            )
        # Track if we're able to do a "fast reinit", which means there were no
        # major changes and can skip all slow initialization steps.
        fast_reinit = \
            not bool(args.get("force_reinit", False)) \
            and not master_clock_rate_changed \
            and not ref_clk_freq_changed
        if fast_reinit:
            self.log.debug(
                "Attempting fast re-init with the following settings: "
                "master_clock_rate={} MHz ref_clk_freq={}"
                .format(
                    self.master_clock_rate / 1e6,
                    self.ref_clock_freq,
                )
            )
        # Note: MagnesiumInitManager.init() can still override fast_reinit.
        # Consider it a hint.
        result = MagnesiumInitManager(self, self._spi_ifaces).init(
            args, self._init_args, fast_reinit)
        if result:
            self._init_args = args
        return result

    def get_user_eeprom_data(self):
        """
        Return a dict of blobs stored in the user data section of the EEPROM.
        """
        return {
            blob_id: self.eeprom_fs.get_blob(blob_id)
            for blob_id in iterkeys(self.eeprom_fs.entries)
        }

    def set_user_eeprom_data(self, eeprom_data):
        """
        Update the local EEPROM with the data from eeprom_data.

        The actual writing to EEPROM can take some time, and is thus kicked
        into a background task. Don't call set_user_eeprom_data() quickly in
        succession. Also, while the background task is running, reading the
        EEPROM is unavailable and MPM won't be able to reboot until it's
        completed.
        However, get_user_eeprom_data() will immediately return the correct
        data after this method returns.
        """
        for blob_id, blob in iteritems(eeprom_data):
            self.eeprom_fs.set_blob(blob_id, blob)
        self.log.trace("Writing EEPROM info to `{}'".format(self.eeprom_path))
        eeprom_offset = self.user_eeprom[self.rev]['offset']
        def _write_to_eeprom_task(path, offset, data, log):
            " Writer task: Actually write to file "
            # Note: This can be sped up by only writing sectors that actually
            # changed. To do so, this function would need to read out the
            # current state of the file, do some kind of diff, and then seek()
            # to the different sectors. When very large blobs are being
            # written, it doesn't actually help all that much, of course,
            # because in that case, we'd anyway be changing most of the EEPROM.
            with open(path, 'r+b') as eeprom_file:
                log.trace("Seeking forward to `{}'".format(offset))
                eeprom_file.seek(eeprom_offset)
                log.trace("Writing a total of {} bytes.".format(
                    len(self.eeprom_fs.buffer)))
                eeprom_file.write(data)
                log.trace("EEPROM write complete.")
        thread_id = "eeprom_writer_task_{}".format(self.slot_idx)
        if any([x.name == thread_id for x in threading.enumerate()]):
            # Should this be fatal?
            self.log.warn("Another EEPROM writer thread is already active!")
        writer_task = threading.Thread(
            target=_write_to_eeprom_task,
            args=(
                self.eeprom_path,
                eeprom_offset,
                self.eeprom_fs.buffer,
                self.log
            ),
            name=thread_id,
        )
        writer_task.start()
        # Now return and let the copy finish on its own. The thread will detach
        # and MPM won't terminate this process until the thread is complete.
        # This does not stop anyone from killing this process (and the thread)
        # while the EEPROM write is happening, though.

    def get_master_clock_rate(self):
        " Return master clock rate (== sampling rate) "
        return self.master_clock_rate

    def update_ref_clock_freq(self, freq):
        """
        Call this function if the frequency of the reference clock changes (the
        10, 20, 25 MHz one). Note: Won't actually re-run any settings.
        """
        assert freq in (10e6, 20e6, 25e6), \
                "Invalid ref clock frequency: {}".format(freq)
        self.log.trace("Changing ref clock frequency to %f MHz", freq/1e6)
        self.ref_clock_freq = freq


    ##########################################################################
    # Sensors
    ##########################################################################
    def get_ref_lock(self):
        """
        Returns True if the LMK reference is locked.

        Note: This does not return a sensor dict. The sensor API call is
        in the motherboard class.
        """
        if self.lmk is None:
            self.log.trace("LMK object not yet initialized, defaulting to " \
                           "no ref locked!")
            return False
        lmk_lock_status = self.lmk.check_plls_locked()
        self.log.trace("LMK lock status is: {}".format(lmk_lock_status))
        return lmk_lock_status

    def get_lowband_lo_lock(self, which):
        """
        Return LO lock status (Boolean!) of the lowband LOs. 'which' must be
        either 'tx' or 'rx'
        """
        assert which.lower() in ('tx', 'rx')
        return self.cpld.get_lo_lock_status(which.upper())

    def get_ad9371_lo_lock(self, which):
        """
        Return LO lock status (Boolean!) of the lowband LOs. 'which' must be
        either 'tx' or 'rx'
        """
        return self.mykonos.get_lo_locked(which.upper())

    def get_lowband_tx_lo_locked_sensor(self, chan):
        " TX lowband LO lock sensor "
        self.log.trace("Querying TX lowband LO lock status for chan %d...",
                       chan)
        lock_status = self.get_lowband_lo_lock('tx')
        return {
            'name': 'lowband_lo_locked',
            'type': 'BOOLEAN',
            'unit': 'locked' if lock_status else 'unlocked',
            'value': str(lock_status).lower(),
        }

    def get_lowband_rx_lo_locked_sensor(self, chan):
        " RX lowband LO lock sensor "
        self.log.trace("Querying RX lowband LO lock status for chan %d...",
                       chan)
        lock_status = self.get_lowband_lo_lock('rx')
        return {
            'name': 'lowband_lo_locked',
            'type': 'BOOLEAN',
            'unit': 'locked' if lock_status else 'unlocked',
            'value': str(lock_status).lower(),
        }

    def get_ad9371_tx_lo_locked_sensor(self, chan):
        " TX ad9371 LO lock sensor "
        self.log.trace("Querying TX AD9371 LO lock status for chan %d...", chan)
        lock_status = self.get_ad9371_lo_lock('tx')
        return {
            'name': 'ad9371_lo_locked',
            'type': 'BOOLEAN',
            'unit': 'locked' if lock_status else 'unlocked',
            'value': str(lock_status).lower(),
        }

    def get_ad9371_rx_lo_locked_sensor(self, chan):
        " RX ad9371 LO lock sensor "
        self.log.trace("Querying RX AD9371 LO lock status for chan %d...", chan)
        lock_status = self.get_ad9371_lo_lock('tx')
        return {
            'name': 'ad9371_lo_locked',
            'type': 'BOOLEAN',
            'unit': 'locked' if lock_status else 'unlocked',
            'value': str(lock_status).lower(),
        }


    ##########################################################################
    # Debug
    ##########################################################################
    def cpld_peek(self, addr):
        """
        Debug for accessing the CPLD via the RPC shell.
        """
        return self.cpld.peek16(addr)

    def cpld_poke(self, addr, data):
        """
        Debug for accessing the CPLD via the RPC shell.
        """
        self.cpld.poke16(addr, data)
        return self.cpld.peek16(addr)

    def dump_jesd_core(self):
        " Debug method to dump all JESD core regs "
        with open_uio(
            label="dboard-regs-{}".format(self.slot_idx),
            read_only=False
        ) as dboard_ctrl_regs:
            for i in range(0x2000, 0x2110, 0x10):
                print(("0x%04X " % i), end=' ')
                for j in range(0, 0x10, 0x4):
                    print(("%08X" % dboard_ctrl_regs.peek32(i + j)), end=' ')
                print("")

    def dbcore_peek(self, addr):
        """
        Debug for accessing the DB Core registers via the RPC shell.
        """
        with open_uio(
            label="dboard-regs-{}".format(self.slot_idx),
            read_only=False
        ) as dboard_ctrl_regs:
            rd_data = dboard_ctrl_regs.peek32(addr)
            self.log.trace("DB Core Register 0x{:04X} response: 0x{:08X}".format(addr, rd_data))
            return rd_data

    def dbcore_poke(self, addr, data):
        """
        Debug for accessing the DB Core registers via the RPC shell.
        """
        with open_uio(
            label="dboard-regs-{}".format(self.slot_idx),
            read_only=False
        ) as dboard_ctrl_regs:
            self.log.trace("Writing DB Core Register 0x{:04X} with 0x{:08X}...".format(addr, data))
            dboard_ctrl_regs.poke32(addr, data)

