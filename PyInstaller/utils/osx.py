#-----------------------------------------------------------------------------
# Copyright (c) 2014-2021, PyInstaller Development Team.
#
# Distributed under the terms of the GNU General Public License (version 2
# or later) with exception for distributing the bootloader.
#
# The full license is in the file COPYING.txt, distributed with this software.
#
# SPDX-License-Identifier: (GPL-2.0-or-later WITH Bootloader-exception)
#-----------------------------------------------------------------------------
"""
Utils for Mac OS platform.
"""

import math
import os
import shutil

from macholib.mach_o import LC_BUILD_VERSION, LC_CODE_SIGNATURE, LC_SEGMENT_64, LC_SYMTAB, LC_VERSION_MIN_MACOSX
from macholib.MachO import MachO

import PyInstaller.log as logging
from PyInstaller.compat import base_prefix, exec_command_all

logger = logging.getLogger(__name__)


def is_homebrew_env():
    """
    Check if Python interpreter was installed via Homebrew command 'brew'.

    :return: True if Homebrew else otherwise.
    """
    # Python path prefix should start with Homebrew prefix.
    env_prefix = get_homebrew_prefix()
    if env_prefix and base_prefix.startswith(env_prefix):
        return True
    return False


def is_macports_env():
    """
    Check if Python interpreter was installed via Macports command 'port'.

    :return: True if Macports else otherwise.
    """
    # Python path prefix should start with Macports prefix.
    env_prefix = get_macports_prefix()
    if env_prefix and base_prefix.startswith(env_prefix):
        return True
    return False


def get_homebrew_prefix():
    """
    :return: Root path of the Homebrew environment.
    """
    prefix = shutil.which('brew')
    # Conversion:  /usr/local/bin/brew -> /usr/local
    prefix = os.path.dirname(os.path.dirname(prefix))
    return prefix


def get_macports_prefix():
    """
    :return: Root path of the Macports environment.
    """
    prefix = shutil.which('port')
    # Conversion:  /usr/local/bin/port -> /usr/local
    prefix = os.path.dirname(os.path.dirname(prefix))
    return prefix


def _find_version_cmd(header):
    """
    Helper that finds the version command in the given MachO header.
    """
    # The SDK version is stored in LC_BUILD_VERSION command (used when targeting the latest versions of macOS) or in
    # older LC_VERSION_MIN_MACOSX command. Check for presence of either.
    version_cmd = [cmd for cmd in header.commands if cmd[0].cmd in {LC_BUILD_VERSION, LC_VERSION_MIN_MACOSX}]
    assert len(version_cmd) == 1, "Expected exactly one LC_BUILD_VERSION or LC_VERSION_MIN_MACOSX command!"
    return version_cmd[0]


def get_macos_sdk_version(filename):
    """
    Obtain the version of macOS SDK against which the given binary was built.

    NOTE: currently, version is retrieved only from the first arch slice in the binary.

    :return: (major, minor, revision) tuple
    """
    binary = MachO(filename)
    header = binary.headers[0]
    # Find version command using helper
    version_cmd = _find_version_cmd(header)
    return _hex_triplet(version_cmd[1].sdk)


def _hex_triplet(version):
    # Parse SDK version number
    major = (version & 0xFF0000) >> 16
    minor = (version & 0xFF00) >> 8
    revision = (version & 0xFF)
    return major, minor, revision


def macosx_version_min(filename: str) -> tuple:
    """
    Get the -macosx-version-min used to compile a macOS binary.

    For fat binaries, the minimum version is selected.
    """
    versions = []
    for header in MachO(filename).headers:
        cmd = _find_version_cmd(header)
        if cmd[0].cmd == LC_VERSION_MIN_MACOSX:
            versions.append(cmd[1].version)
        else:
            # macOS >= 10.14 uses LC_BUILD_VERSION instead.
            versions.append(cmd[1].minos)

    return min(map(_hex_triplet, versions))


def set_macos_sdk_version(filename, major, minor, revision):
    """
    Overwrite the macOS SDK version declared in the given binary with the specified version.

    NOTE: currently, only version in the first arch slice is modified.
    """
    # Validate values
    assert 0 <= major <= 255, "Invalid major version value!"
    assert 0 <= minor <= 255, "Invalid minor version value!"
    assert 0 <= revision <= 255, "Invalid revision value!"
    # Open binary
    binary = MachO(filename)
    header = binary.headers[0]
    # Find version command using helper
    version_cmd = _find_version_cmd(header)
    # Write new SDK version number
    version_cmd[1].sdk = major << 16 | minor << 8 | revision
    # Write changes back.
    with open(binary.filename, 'rb+') as fp:
        binary.write(fp)


def fix_exe_for_code_signing(filename):
    """
    Fixes the Mach-O headers to make code signing possible.

    Code signing on Mac OS does not work out of the box with embedding .pkg archive into the executable.

    The fix is done this way:
    - Make the embedded .pkg archive part of the Mach-O 'String Table'. 'String Table' is at end of the Mac OS exe file,
      so just change the size of the table to cover the end of the file.
    - Fix the size of the __LINKEDIT segment.

    Note: the above fix works only if the single-arch thin executable or the last arch slice in a multi-arch fat
    executable is not signed, because LC_CODE_SIGNATURE comes after LC_SYMTAB, and because modification of headers
    invalidates the code signature. On modern arm64 macOS, code signature is mandatory, and therefore compilers
    create a dummy signature when executable is built. In such cases, that signature needs to be removed before this
    function is called.

    Mach-O format specification: http://developer.apple.com/documentation/Darwin/Reference/ManPages/man5/Mach-O.5.html
    """
    # Estimate the file size after data was appended
    file_size = os.path.getsize(filename)

    # Take the last available header. A single-arch thin binary contains a single slice, while a multi-arch fat binary
    # contains multiple, and we need to modify the last one, which is adjacent to the appended data.
    executable = MachO(filename)
    header = executable.headers[-1]

    # Sanity check: ensure the executable slice is not signed (otherwise signature's section comes last in the
    # __LINKEDIT segment).
    sign_sec = [cmd for cmd in header.commands if cmd[0].cmd == LC_CODE_SIGNATURE]
    assert len(sign_sec) == 0, "Executable contains code signature!"

    # Find __LINKEDIT segment by name (16-byte zero padded string)
    __LINKEDIT_NAME = b'__LINKEDIT\x00\x00\x00\x00\x00\x00'
    linkedit_seg = [cmd for cmd in header.commands if cmd[0].cmd == LC_SEGMENT_64 and cmd[1].segname == __LINKEDIT_NAME]
    assert len(linkedit_seg) == 1, "Expected exactly one __LINKEDIT segment!"
    linkedit_seg = linkedit_seg[0][1]  # Take the segment command entry
    # Find SYMTAB section
    symtab_sec = [cmd for cmd in header.commands if cmd[0].cmd == LC_SYMTAB]
    assert len(symtab_sec) == 1, "Expected exactly one SYMTAB section!"
    symtab_sec = symtab_sec[0][1]  # Take the symtab command entry

    # The string table is located at the end of the SYMTAB section, which in turn is the last section in the __LINKEDIT
    # segment. Therefore, the end of SYMTAB section should be aligned with the end of __LINKEDIT segment, and in turn
    # both should be aligned with the end of the file (as we are in the last or the only arch slice).
    #
    # However, when removing the signature from the executable using codesign under Mac OS 10.13, the codesign utility
    # may produce an invalid file, with the declared length of the __LINKEDIT segment (linkedit_seg.filesize) pointing
    # beyond the end of file, as reported in issue #6167.
    #
    # We can compensate for that by not using the declared sizes anywhere, and simply recompute them. In the final
    # binary, the __LINKEDIT segment and the SYMTAB section MUST end at the end of the file (otherwise, we have bigger
    # issues...). So simply recompute the declared sizes as difference between the final file length and the
    # corresponding start offset (NOTE: the offset is relative to start of the slice, which is stored in header.offset.
    # In thin binaries, header.offset is zero and start offset is relative to the start of file, but with fat binaries,
    # header.offset is non-zero)
    symtab_sec.strsize = file_size - (header.offset + symtab_sec.stroff)
    linkedit_seg.filesize = file_size - (header.offset + linkedit_seg.fileoff)

    # Compute new vmsize by rounding filesize up to full page size.
    page_size = (0x4000 if _get_arch_string(header.header).startswith('arm64') else 0x1000)
    linkedit_seg.vmsize = math.ceil(linkedit_seg.filesize / page_size) * page_size

    # NOTE: according to spec, segments need to be aligned to page boundaries: 0x4000 (16 kB) for arm64, 0x1000 (4 kB)
    # for other arches. But it seems we can get away without rounding and padding the segment file size - perhaps
    # because it is the last one?

    # Write changes
    with open(filename, 'rb+') as fp:
        executable.write(fp)

    # In fat binaries, we also need to adjust the fat header. macholib as of version 1.14 does not support this, so we
    # need to do it ourselves...
    if executable.fat:
        from macholib.mach_o import (FAT_MAGIC, FAT_MAGIC_64, fat_arch, fat_arch64, fat_header)
        with open(filename, 'rb+') as fp:
            # Taken from MachO.load_fat() implementation. The fat header's signature has already been validated when we
            # loaded the file for the first time.
            fat = fat_header.from_fileobj(fp)
            if fat.magic == FAT_MAGIC:
                archs = [fat_arch.from_fileobj(fp) for i in range(fat.nfat_arch)]
            elif fat.magic == FAT_MAGIC_64:
                archs = [fat_arch64.from_fileobj(fp) for i in range(fat.nfat_arch)]
            # Adjust the size in the fat header for the last slice.
            arch = archs[-1]
            arch.size = file_size - arch.offset
            # Now write the fat headers back to the file.
            fp.seek(0)
            fat.to_fileobj(fp)
            for arch in archs:
                arch.to_fileobj(fp)


def _get_arch_string(header):
    """
    Converts cputype and cpusubtype from mach_o.mach_header_64 into arch string comparible with lipo/codesign.
    The list of supported architectures can be found in man(1) arch.
    """
    # NOTE: the constants below are taken from macholib.mach_o
    cputype = header.cputype
    cpusubtype = header.cpusubtype & 0x0FFFFFFF
    if cputype == 0x01000000 | 7:
        if cpusubtype == 8:
            return 'x86_64h'  # 64-bit intel (haswell)
        else:
            return 'x86_64'  # 64-bit intel
    elif cputype == 0x01000000 | 12:
        if cpusubtype == 2:
            return 'arm64e'
        else:
            return 'arm64'
    elif cputype == 7:
        return 'i386'  # 32-bit intel
    assert False, 'Unhandled architecture!'


class InvalidBinaryError(Exception):
    """
    Exception raised by ˙get_binary_architectures˙ when it is passed an invalid binary.
    """
    pass


class IncompatibleBinaryArchError(Exception):
    """
    Exception raised by `binary_to_target_arch` when the passed binary fails the strict architecture check.
    """
    pass


def get_binary_architectures(filename):
    """
    Inspects the given binary and returns tuple (is_fat, archs), where is_fat is boolean indicating fat/thin binary,
    and arch is list of architectures with lipo/codesign compatible names.
    """
    try:
        executable = MachO(filename)
    except ValueError as e:
        raise InvalidBinaryError("Invalid Mach-O binary!") from e
    return bool(executable.fat), [_get_arch_string(hdr.header) for hdr in executable.headers]


def convert_binary_to_thin_arch(filename, thin_arch):
    """
    Convert the given fat binary into thin one with the specified target architecture.
    """
    cmd_args = ['lipo', '-thin', thin_arch, filename, '-output', filename]
    retcode, stdout, stderr = exec_command_all(*cmd_args)
    if retcode != 0:
        logger.warning(
            "lipo command (%r) failed with error code %d!\nstdout: %r\nstderr: %r", cmd_args, retcode, stdout, stderr
        )
        raise SystemError("lipo failure!")


def binary_to_target_arch(filename, target_arch, display_name=None):
    """
    Check that the given binary contains required architecture slice(s) and convert the fat binary into thin one,
    if necessary.
    """
    if not display_name:
        display_name = filename  # Same as input file
    # Check the binary
    is_fat, archs = get_binary_architectures(filename)
    if target_arch == 'universal2':
        if not is_fat:
            raise IncompatibleBinaryArchError(f"{display_name} is not a fat binary!")
        # Assume fat binary is universal2; nothing to do
    else:
        if is_fat:
            if target_arch not in archs:
                raise IncompatibleBinaryArchError(f"{display_name} does not contain slice for {target_arch}!")
            # Convert to thin arch
            logger.debug("Converting fat binary %s (%s) to thin binary (%s)", filename, display_name, target_arch)
            convert_binary_to_thin_arch(filename, target_arch)
        else:
            if target_arch not in archs:
                raise IncompatibleBinaryArchError(
                    f"{display_name} is incompatible with target arch {target_arch} (has arch: {archs[0]})!"
                )
            # Binary has correct arch; nothing to do


def remove_signature_from_binary(filename):
    """
    Remove the signature from all architecture slices of the given binary file using the codesign utility.
    """
    logger.debug("Removing signature from file %r", filename)
    cmd_args = ['codesign', '--remove', '--all-architectures', filename]
    retcode, stdout, stderr = exec_command_all(*cmd_args)
    if retcode != 0:
        logger.warning(
            "codesign command (%r) failed with error code %d!\n"
            "stdout: %r\n"
            "stderr: %r", cmd_args, retcode, stdout, stderr
        )
        raise SystemError("codesign failure!")


def sign_binary(filename, identity=None, entitlements_file=None, deep=False):
    """
    Sign the binary using codesign utility. If no identity is provided, ad-hoc signing is performed.
    """
    extra_args = []
    if not identity:
        identity = '-'  # ad-hoc signing
    else:
        extra_args.append('--options=runtime')  # hardened runtime
    if entitlements_file:
        extra_args.append('--entitlements')
        extra_args.append(entitlements_file)
    if deep:
        extra_args.append('--deep')

    logger.debug("Signing file %r", filename)
    cmd_args = ['codesign', '-s', identity, '--force', '--all-architectures', '--timestamp', *extra_args, filename]
    retcode, stdout, stderr = exec_command_all(*cmd_args)
    if retcode != 0:
        logger.warning(
            "codesign command (%r) failed with error code %d!\n"
            "stdout: %r\n"
            "stderr: %r", cmd_args, retcode, stdout, stderr
        )
        raise SystemError("codesign failure!")
