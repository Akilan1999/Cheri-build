#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
#
# SPDX-License-Identifier: BSD-2-Clause
#
# Copyright (c) 2020 Alex Richardson
#
# This work was supported by Innovate UK project 105694, "Digital Security by
# Design (DSbD) Technology Platform Prototype".
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
import argparse
import datetime
import os
import shlex
import shutil
import sys
import tempfile
import time
import typing
from pathlib import Path

_cheribuild_root = Path(__file__).resolve().parent
_pexpect_dir = _cheribuild_root / "3rdparty/pexpect"
assert (_pexpect_dir / "pexpect/__init__.py").exists()
sys.path.insert(1, str(_pexpect_dir))
sys.path.insert(1, str(_pexpect_dir.parent / "ptyprocess"))
sys.path.insert(1, str(_cheribuild_root))
from pycheribuild.utils import ConfigBase, fatal_error, get_global_config, init_global_config
from pycheribuild.config.compilation_targets import CompilationTargets
from pycheribuild.processutils import print_command
from pycheribuild.boot_cheribsd import (boot_and_login, CheriBSDInstance, CheriBSDSpawnMixin, failure, PretendSpawn,
                                        success, pexpect)
from pycheribuild.filesystemutils import FileSystemUtils

from serial.tools.list_ports import comports
from serial.tools.list_ports_common import ListPortInfo

VIVADO_SCRIPT = b"""
# Setup some variables
#

if { [llength $argv] != 2 } {
    puts "ERROR!! Did not pass proper number of arguments to this script."
    puts "args: <bitfile path> <ltxfile path>"
    exit -1
}
set bitfile [lindex $argv 0]
set probfile [lindex $argv 1]

open_hw
connect_hw_server
open_hw_target
current_hw_device [get_hw_devices xcvu9p_0]
# refresh_hw_device -update_hw_probes false [lindex [get_hw_devices xcvu9p_0] 0]
set_property PROBES.FILE $probfile [get_hw_devices xcvu9p_0]
set_property FULL_PROBES.FILE $probfile [get_hw_devices xcvu9p_0]
set_property PROGRAM.FILE $bitfile [get_hw_devices xcvu9p_0]
puts "---------------------"
puts "Program Configuration"
puts "---------------------"
puts "Bitstream : $bitfile"
puts "Probe Info: $probfile"
puts ""
puts "Programming..."
program_hw_devices [get_hw_devices xcvu9p_0]
close_hw_target
disconnect_hw_server
close_hw
puts "Done!"
exit 0
"""

OPENOCD_SCIPRT = b"""
interface ftdi
transport select jtag
bindto 0.0.0.0
adapter_khz 2000

ftdi_tdo_sample_edge falling

ftdi_vid_pid 0x0403 0x6014

ftdi_channel 0
ftdi_layout_init 0x00e8 0x60eb

reset_config none

set _CHIPNAME riscv
jtag newtap $_CHIPNAME cpu -irlen 18 -ignore-version -expected-id 0x04B31093

set _TARGETNAME $_CHIPNAME.cpu
target create $_TARGETNAME riscv -chain-position $_TARGETNAME

riscv set_ir dtmcs 0x022924
riscv set_ir dmi 0x003924

init

halt
reset halt
"""


def load_bitfile(bitfile: Path, ltxfile: Path, fu: FileSystemUtils):
    if shutil.which("vivado") is None:
        fatal_error("vivado not in $PATH, cannot continue")

    with tempfile.NamedTemporaryFile() as t:
        t.write(VIVADO_SCRIPT)
        t.flush()
        args = ["vivado", "-nojournal", "-notrace", "-nolog",
                "-source", t.name, "-mode", "batch", "-tclargs",
                str(bitfile), str(ltxfile)]
        print_command(args, config=get_global_config())
        if get_global_config().pretend:
            vivado = PretendSpawn(args[0], args[1:])
        else:
            vivado = pexpect.spawn(args[0], args[1:], logfile=sys.stdout, encoding="utf-8")
        vivado_exit_str = "Exiting Vivado at"
        if vivado.expect_exact(["****** Vivado", vivado_exit_str]) != 0:
            failure("Vivado failed to start", exit=True)
        success("Vivado started")
        if vivado.expect_exact(["Programming...", vivado_exit_str]) != 0:
            failure("Vivado failed to start programming", exit=True)
        success("Vivado started programming FPGA")
        # 5 minutes should be enough time to programt the FPGA
        if vivado.expect_exact(["Done!", vivado_exit_str], timeout=5 * 60) != 0:
            failure("Vivado failed to program FPGA", exit=True)
        success("Vivado finished programming FPGA")
        vivado.expect_exact([vivado_exit_str])
        vivado.wait()
        fu.delete_file(Path("webtalk.log"), print_verbose_only=True)
        fu.delete_file(Path("webtalk.jou"), print_verbose_only=True)
        if not get_global_config().pretend:
            # wait for 3 seconds to avoid 'Error: libusb_claim_interface() failed with LIBUSB_ERROR_BUSY'
            time.sleep(3)


def abspath_arg(s) -> Path:
    return Path(os.path.abspath(os.path.expandvars(os.path.expanduser(s))))


class FakeSerialSpawn(CheriBSDSpawnMixin, PretendSpawn):
    pass


class FpgaConnection:
    """Access to openOCD+GDB+Serial port connection"""

    def __init__(self, gdb: pexpect.spawn, openocd: pexpect.spawn, serial: CheriBSDSpawnMixin):
        self.gdb = gdb
        self.openocd = openocd
        self.serial = serial


def reset_soc(conn: FpgaConnection):
    # On the rare occasion you need to reset SoC stuff not just the core, set *(0x6fff0000)=1 does a write to a GPIO
    # block whose output is connected to the SoC's reset so that lets you reset the whole SoC
    conn.gdb.sendline("set *(0x6fff0000)=1")
    # though the core will then be running so you'll need to c and then ^C in GDB to get things back in sync
    conn.gdb.sendline("continue")
    conn.gdb.sendintr()


def start_openocd(openocd_cmd: Path) -> typing.Tuple[pexpect.spawn, int]:
    with tempfile.NamedTemporaryFile() as t:
        t.write(OPENOCD_SCIPRT)
        t.flush()
        cmdline = [str(openocd_cmd), "-f", t.name]
        print_command(cmdline, config=get_global_config())
        if get_global_config().pretend:
            openocd = PretendSpawn(cmdline[0], cmdline[1:])
        else:
            openocd = pexpect.spawn(cmdline[0], cmdline[1:], logfile=sys.stdout, encoding="utf-8")
        openocd.expect_exact(["Open On-Chip Debugger"])
        success("openocd started")
        gdb_port = 3333
        openocd.expect(["Info : Listening on port (\\d+) for gdb connections"])
        if openocd.match is not None:
            gdb_port = int(openocd.match.group(1))
        openocd.expect_exact(["Info : Listening on port 4444 for telnet connections"])
        success("openocd waiting for GDB connection")
        return openocd, gdb_port


def load_and_start_kernel(*, gdb_cmd: Path, openocd_cmd: Path, bios_image: Path, kernel_image: Path = None,
                          kernel_debug_file: Path = None, tty_info: ListPortInfo) -> FpgaConnection:
    # Open the serial connection first to check that it's available:
    # We use the miniterm command bundled with PySerial as the interactive prompt.
    # This means that we don't depend on minicom/picocom being installed.
    success("Connecting to TTY...")
    miniterm_cmd = ["-m", "serial.tools.miniterm", tty_info.device, "115200", "--filter", "colorize"]
    if get_global_config().pretend:
        serial_conn = FakeSerialSpawn(sys.executable, miniterm_cmd)
    else:
        serial_conn = CheriBSDInstance(CompilationTargets.CHERIBSD_RISCV_HYBRID, sys.executable, miniterm_cmd,
                                       logfile=sys.stdout, encoding="utf-8", timeout=60)
    serial_conn.expect(["--- Miniterm on "])
    success("Connected to TTY")
    # First start openocd
    gdb_start_time = datetime.datetime.utcnow()
    openocd, openocd_gdb_port = start_openocd(openocd_cmd)
    # openocd is running, now start GDB
    args = [str(Path(bios_image).absolute()),
            "-ex", "target extended-remote :" + str(openocd_gdb_port)]
    args += ["-ex", "set confirm off"]  # avoid interactive prompts
    args += ["-ex", "monitor reset init"]  # reset and go back to boot room
    args += ["-ex", "si 5"]  # we need to run the first few instructions to get a valid DTB
    args += ["-ex", "set disassemble-next-line on"]
    # Load the kernel image first since load changes the next PC to the entry point
    if kernel_image is not None:
        kernel_image = kernel_image.absolute()
        if kernel_debug_file is None:
            # If there is a .full image use that to get debug symbols:
            full_file = kernel_image.with_name(kernel_image.name + ".full")
            if full_file.exists():
                kernel_debug_file = full_file
            else:
                # Fall back to the kernel image without debug info.
                kernel_debug_file = kernel_image
        args += ["-ex", "symbol-file " + shlex.quote(str(kernel_debug_file.absolute()))]
        args += ["-ex", "load " + shlex.quote(str(kernel_image.absolute()))]
    args += ["-ex", "load " + shlex.quote(str(Path(bios_image).absolute()))]
    print_command(str(gdb_cmd), *args, config=get_global_config())
    if get_global_config().pretend:
        gdb = PretendSpawn(str(gdb_cmd), args, timeout=60)
    else:
        gdb = pexpect.spawn(str(gdb_cmd), args, timeout=60, logfile=sys.stdout, encoding="utf-8")
    gdb.expect_exact(["Reading symbols from"])
    # openOCD should acknowledge the GDB connection:
    openocd.expect_exact(["Info : accepting 'gdb' connection on tcp/{}".format(openocd_gdb_port)])
    success("openocd accepted GDB connection")
    gdb.expect_exact(["Remote debugging using :" + str(openocd_gdb_port)])
    success("GDB connected to openocd")
    gdb.expect_exact(["0x0000000070000000 in ??"])
    success("PC set to bootrom")
    gdb.expect_exact(["0x0000000044000000 in ??"])
    success("Done executing bootrom")
    if kernel_image is not None:
        gdb.expect_exact(["Loading section .text"])
        load_start_time = datetime.datetime.utcnow()
        success("Started loading kernel image (this may take a long time)")
        gdb.expect_exact(["Transfer rate:"], timeout=20 * 60)  # XXX: is 20 minutes a sensible timeout?
        load_end_time = datetime.datetime.utcnow()
        success("Finished loading kernel image in ", load_end_time - load_start_time)
    # Now load the bootloader
    gdb.expect_exact(["Loading section .text"])
    load_start_time = datetime.datetime.utcnow()
    success("Started loading bootloader image")
    gdb.expect_exact(["Transfer rate:"], timeout=10 * 60)  # XXX: is 10 minutes a sensible timeout?
    load_end_time = datetime.datetime.utcnow()
    success("Finished loading bootloader image in ", load_end_time - load_start_time)
    gdb_finish_time = load_end_time
    gdb.sendline("continue")
    success("Starting CheriBSD after ", datetime.datetime.utcnow() - gdb_start_time)
    # TODO: network_iface="xae0", but DHCP doesn't work
    boot_and_login(serial_conn, starttime=gdb_finish_time, network_iface=None)
    return FpgaConnection(gdb, openocd, serial_conn)


def find_vcu118_tty() -> ListPortInfo:
    # find the serial port:
    expected_vendor_id = 0x10c4
    expected_product_id = 0xea70
    for info in comports(include_links=True):
        assert isinstance(info, ListPortInfo)
        if info.pid == expected_product_id and info.vid == expected_vendor_id:
            return info
    raise ValueError("Could not find USB TTY with VID", hex(expected_vendor_id), "PID", hex(expected_product_id))


def main():
    # noinspection PyTypeChecker
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--bitfile", help="The bitfile to load", type=abspath_arg)
    parser.add_argument("--ltxfile", help="The LTX file to use", type=abspath_arg)
    parser.add_argument("--bios", default="openocd", help="The machine-mode program to load", required=True,
                        type=abspath_arg)
    parser.add_argument("--kernel", help="The supervisor-mode program to load", type=abspath_arg)
    parser.add_argument("--kernel-debug-file", help="Debug info file for the kernel", type=abspath_arg)
    parser.add_argument("--gdb", default=shutil.which("gdb") or "gdb", help="Path to GDB binary", type=Path)
    parser.add_argument("--openocd", default=shutil.which("openocd") or "openocd", help="Path to openocd binary",
                        type=abspath_arg)
    parser.add_argument("--pretend", help="Don't actually run the commands just show what would happen",
                        action="store_true")
    try:
        # noinspection PyUnresolvedReferences
        import argcomplete
        argcomplete.autocomplete(parser)
    except ImportError:
        pass
    args = parser.parse_args()
    print(args)
    init_global_config(ConfigBase(pretend=args.pretend, verbose=True, quiet=False))
    if args.bitfile is not None:
        if args.ltxfile is None:
            args.ltxfile = Path(args.bitfile).with_suffix(".ltx")
        load_bitfile(args.bitfile, args.ltxfile, FileSystemUtils(get_global_config()))

    tty_info = find_vcu118_tty()
    print("Found TTY:", tty_info)
    print(tty_info.usb_info())
    conn = load_and_start_kernel(gdb_cmd=args.gdb, openocd_cmd=args.openocd, bios_image=args.bios,
                                 kernel_image=args.kernel, kernel_debug_file=args.kernel_debug_file, tty_info=tty_info)
    success("Interacting with CheriBSD. Press CTRL+] to exit")
    # Print the help message
    conn.serial.sendcontrol('t')
    conn.serial.sendcontrol('h')
    # interac() prints all input+output -> disable logfile
    conn.serial.logfile = None
    conn.serial.logfile_read = None
    conn.serial.logfile_send = None
    conn.serial.interact()


if __name__ == "__main__":
    main()