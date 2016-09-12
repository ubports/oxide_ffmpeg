#!/usr/bin/python
#
# Copyright (c) 2012 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Creates a GN include file for building FFmpeg from source.

The way this works is a bit silly but it's easier than reverse engineering
FFmpeg's configure scripts and Makefiles and manually maintaining chromium
build files. It scans through build directories for object files then does a
reverse lookup against the FFmpeg source tree to find the corresponding C or
assembly file.

Running build_ffmpeg.py on each supported platform for all architectures is
required prior to running this script.  See build_ffmpeg.py for details as well
as the documentation at:

https://docs.google.com/document/d/14bqZ9NISsyEO3948wehhJ7wc9deTIz-yHUhF1MQp7Po/edit

Once you've built all platforms and architectures you may run this script.
"""

__author__ = 'scherkus@chromium.org (Andrew Scherkus)'

import collections
import copy
import datetime
from enum import enum
import fnmatch
import credits_updater
import itertools
import optparse
import os
import re
import shutil
import string
import subprocess
import sys

COPYRIGHT = """# Copyright %d The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# NOTE: this file is autogenerated by ffmpeg/chromium/scripts/generate_gn.py

""" % (datetime.datetime.now().year)

GN_HEADER = """import("//build/config/arm.gni")
import("ffmpeg_options.gni")

# Declare empty versions of each variable for easier +=ing later.
ffmpeg_c_sources = []
ffmpeg_gas_sources = []
ffmpeg_yasm_sources = []

"""
GN_CONDITION_BEGIN = """if (%s) {
"""
GN_CONDITION_END = """}

"""
GN_C_SOURCES_BEGIN = """ffmpeg_c_sources += [
"""
GN_GAS_SOURCES_BEGIN = """ffmpeg_gas_sources += [
"""
GN_YASM_SOURCES_BEGIN = """ffmpeg_yasm_sources += [
"""
GN_SOURCE_ITEM = """  "%s",
"""
GN_SOURCE_END = """]
"""

# Controls conditional stanza generation.
Attr = enum('ARCHITECTURE', 'TARGET', 'PLATFORM')
SUPPORT_MATRIX = {
    Attr.ARCHITECTURE:
        set(['ia32', 'x64', 'arm', 'arm64', 'arm-neon', 'mipsel', 'mips64el']),
    Attr.TARGET: set(['Chromium', 'Chrome', 'ChromiumOS', 'ChromeOS']),
    Attr.PLATFORM: set(['android', 'linux', 'win', 'mac'])
}


def NormalizeFilename(name):
  """Removes leading path separators in an attempt to normalize paths."""
  return string.lstrip(name, os.sep)


def CleanObjectFiles(object_files):
  """Removes unneeded object files due to linker errors, binary size, etc...

  Args:
    object_files: List of object files that needs cleaning.
  """
  blacklist = [
      'libavcodec/inverse.o',  # Includes libavutil/inverse.c
      'libavcodec/file_open.o',  # Includes libavutil/file_open.c
      'libavcodec/log2_tab.o',  # Includes libavutil/log2_tab.c
      'libavformat/golomb_tab.o',  # Includes libavcodec/golomb.c
      'libavformat/log2_tab.o',  # Includes libavutil/log2_tab.c
      'libavformat/file_open.o',  # Includes libavutil/file_open.c

      # The following files are removed to trim down on binary size.
      # TODO(ihf): Warning, it is *easy* right now to remove more files
      # than is healthy and end up with a library that the linker does
      # not complain about but that can't be loaded. Add some verification!
      'libavcodec/audioconvert.o',
      'libavcodec/resample.o',
      'libavcodec/resample2.o',
      'libavcodec/x86/dnxhd_mmx.o',
      'libavformat/sdp.o',
      'libavutil/adler32.o',
      'libavutil/audio_fifo.o',
      'libavutil/blowfish.o',
      'libavutil/cast5.o',
      'libavutil/des.o',
      'libavutil/file.o',
      'libavutil/hash.o',
      'libavutil/hmac.o',
      'libavutil/lls.o',
      'libavutil/murmur3.o',
      'libavutil/rc4.o',
      'libavutil/ripemd.o',
      'libavutil/sha512.o',
      'libavutil/tree.o',
      'libavutil/xtea.o',
      'libavutil/xga_font_data.o',
  ]
  for name in blacklist:
    name = name.replace('/', os.sep)
    if name in object_files:
      object_files.remove(name)
  return object_files


def IsAssemblyFile(f):
  _, ext = os.path.splitext(f)
  return ext in ['.S', '.asm']


def IsGasFile(f):
  _, ext = os.path.splitext(f)
  return ext in ['.S']


def IsYasmFile(f):
  _, ext = os.path.splitext(f)
  return ext in ['.asm']


def IsCFile(f):
  _, ext = os.path.splitext(f)
  return ext in ['.c']


def IsSourceFile(f):
  return IsAssemblyFile(f) or IsCFile(f)


def GetSourceFiles(source_dir):
  """Returns a list of source files for the given source directory.

  Args:
    source_dir: Path to build a source mapping for.

  Returns:
    A python list of source file paths.
  """

  def IsSourceDir(d):
    return d != '.git'

  source_files = []
  for root, dirs, files in os.walk(source_dir):
    dirs = filter(IsSourceDir, dirs)
    files = filter(IsSourceFile, files)

    # Strip leading source_dir from root.
    root = root[len(source_dir):]
    source_files.extend([NormalizeFilename(os.path.join(root, name)) for name in
                         files])
  return source_files


def GetObjectFiles(build_dir):
  """Returns a list of object files for the given build directory.

  Args:
    build_dir: Path to build an object file list for.

  Returns:
    A python list of object files paths.
  """
  object_files = []
  for root, _, files in os.walk(build_dir):
    # Strip leading build_dir from root.
    root = root[len(build_dir):]

    for name in files:
      _, ext = os.path.splitext(name)
      if ext == '.o':
        name = NormalizeFilename(os.path.join(root, name))
        object_files.append(name)
  CleanObjectFiles(object_files)
  return object_files


def GetObjectToSourceMapping(source_files):
  """Returns a map of object file paths to source file paths.

  Args:
    source_files: List of source file paths.

  Returns:
    Map with object file paths as keys and source file paths as values.
  """
  object_to_sources = {}
  for name in source_files:
    basename, _ = os.path.splitext(name)
    key = basename + '.o'
    object_to_sources[key] = name
  return object_to_sources


def GetSourceFileSet(object_to_sources, object_files):
  """Determines set of source files given object files.

  Args:
    object_to_sources: A dictionary of object to source file paths.
    object_files: A list of object file paths.

  Returns:
    A python set of source files required to build said objects.
  """
  source_set = set()
  for name in object_files:
    # Intentially raise a KeyError if lookup fails since something is messed
    # up with our source and object lists.
    source_set.add(object_to_sources[name])
  return source_set

SourceListCondition = collections.namedtuple('SourceListCondition',
                                             [Attr.ARCHITECTURE,
                                              Attr.TARGET,
                                              Attr.PLATFORM])


class SourceSet(object):
  """A SourceSet represents a set of source files that are built on each of the
  given set of SourceListConditions.
  """

  def __init__(self, sources, conditions):
    """Creates a SourceSet.

    Args:
      sources: a python set of source files
      conditions: a python set of SourceListConditions where the given sources
        are to be used.
    """
    self.sources = sources
    self.conditions = conditions

  def __repr__(self):
    return '{%s, %s}' % (self.sources, self.conditions)

  def __eq__(self, other):
    return (self.sources == other.sources and
            self.conditions == other.conditions)

  def __hash__(self):
    return hash((frozenset(self.sources), frozenset(self.conditions)))

  def Intersect(self, other):
    """Return a new SourceSet containing the set of source files common to both
    this and the other SourceSet.

    The resulting SourceSet represents the union of the architectures and
    targets of this and the other SourceSet.
    """
    return SourceSet(self.sources & other.sources,
                     self.conditions | other.conditions)

  def Difference(self, other):
    """Return a new SourceSet containing the set of source files not present in
    the other SourceSet.

    The resulting SourceSet represents the intersection of the
    SourceListConditions from this and the other SourceSet.
    """
    return SourceSet(self.sources - other.sources,
                     self.conditions & other.conditions)

  def IsEmpty(self):
    """An empty SourceSet is defined as containing no source files or no
    conditions (i.e., a set of files that aren't built on anywhere).
    """
    return (len(self.sources) == 0 or len(self.conditions) == 0)

  def GenerateGnStanza(self):
    """Generates a gn conditional stanza representing this source set.
    """

    conjunctions = []
    for condition in self.conditions:
      if condition.ARCHITECTURE == '*':
        arch_condition = None
      elif condition.ARCHITECTURE == 'arm-neon':
        arch_condition = 'current_cpu == "arm" && arm_use_neon'
      elif condition.ARCHITECTURE == 'ia32':
        arch_condition = 'current_cpu == "x86"'
      else:
        arch_condition = 'current_cpu == "%s"' % condition.ARCHITECTURE

      # Branding conditions look like:
      #   ffmpeg_branding == "Chrome"
      if condition.TARGET == '*':
        target_condition = None
      else:
        target_condition = 'ffmpeg_branding == "%s"' % condition.TARGET

      # Platform conditions look like:
      #   is_mac
      if condition.PLATFORM == '*':
        platform_condition = None
      else:
        platform_condition = 'is_%s' % condition.PLATFORM

      conjunction_parts = filter(
          None, [platform_condition, arch_condition, target_condition])
      conjunctions.append(' && '.join(conjunction_parts))

    # If there is more that one clause, wrap various conditions in parens
    # before joining.
    if len(conjunctions) > 1:
      conjunctions = ['(%s)' % x for x in conjunctions]

    # Sort conjunctions to make order deterministic.
    joined_conjuctions = ' || '.join(sorted(conjunctions))

    stanza = ''
    # Output a conditional wrapper around stanzas if necessary.
    if joined_conjuctions:
      stanza += GN_CONDITION_BEGIN % joined_conjuctions

      def indent(s):
        return '  %s' % s
    else:
      def indent(s):
        return s

    sources = sorted(n.replace('\\', '/') for n in self.sources)

    # Write out all C sources.
    c_sources = filter(IsCFile, sources)
    if c_sources:
      stanza += indent(GN_C_SOURCES_BEGIN)
      for name in c_sources:
        stanza += indent(GN_SOURCE_ITEM % (name))
      stanza += indent(GN_SOURCE_END)

    # Write out all assembly sources.
    gas_sources = filter(IsGasFile, sources)
    if gas_sources:
      stanza += indent(GN_GAS_SOURCES_BEGIN)
      for name in gas_sources:
        stanza += indent(GN_SOURCE_ITEM % (name))
      stanza += indent(GN_SOURCE_END)

    # Write out all assembly sources.
    yasm_sources = filter(IsYasmFile, sources)
    if yasm_sources:
      stanza += indent(GN_YASM_SOURCES_BEGIN)
      for name in yasm_sources:
        stanza += indent(GN_SOURCE_ITEM % (name))
      stanza += indent(GN_SOURCE_END)

    # Close the conditional if necessary.
    if joined_conjuctions:
      stanza += GN_CONDITION_END
    else:
      stanza += '\n'  # Makeup the spacing for the remove conditional.
    return stanza


def CreatePairwiseDisjointSets(sets):
  """Given a list of SourceSet objects, returns the pairwise disjoint sets.

  NOTE: This isn't the most efficient algorithm, but given how infrequent we
  need to run this and how small the input size is we'll leave it as is.
  """

  disjoint_sets = list(sets)

  new_sets = True
  while new_sets:
    new_sets = False
    for pair in itertools.combinations(disjoint_sets, 2):
      intersection = pair[0].Intersect(pair[1])

      # Both pairs are already disjoint, nothing to do.
      if intersection.IsEmpty():
        continue

      # Add the resulting intersection set.
      new_sets = True
      disjoint_sets.append(intersection)

      # Calculate the resulting differences for this pair of sets.
      #
      # If the differences are an empty set, remove them from the list of sets,
      # otherwise update the set itself.
      for p in pair:
        i = disjoint_sets.index(p)
        difference = p.Difference(intersection)
        if difference.IsEmpty():
          del disjoint_sets[i]
        else:
          disjoint_sets[i] = difference

      # Restart the calculation since the list of disjoint sets has changed.
      break

  return disjoint_sets


def GetAllMatchingConditions(conditions, condition_to_match):
  """Given a set of conditions, find those that match the condition_to_match.
  Matches are found when all attributes of the condition have the same value as
  the condition_to_match, or value is accepted for wildcard attributes within
  condition_to_match.
  """

  found_matches = set()

  # Check all attributes of condition for matching values.
  def accepts_all_values(attribute):
    return getattr(condition_to_match, attribute) == '*'
  attributes_to_check = [a for a in Attr if not accepts_all_values(a)]

  # If all attributes allow wildcard, all conditions are considered matching
  if not attributes_to_check:
    return conditions

  # Check all conditions and accumulate matches.
  for condition in conditions:
    condition_matches = True
    for attribute in attributes_to_check:
      if (getattr(condition, attribute)
          != getattr(condition_to_match, attribute)):
        condition_matches = False
        break
    if condition_matches:
      found_matches.add(condition)

  return found_matches

def GetAttributeValuesRange(attribute, condition):
  """Get the range of values for the given attribute considering the values
  of all attributes in the given condition."""
  if getattr(condition, attribute) == '*':
    values = copy.copy(SUPPORT_MATRIX[attribute])
  else:
    values = set([getattr(condition, attribute)])

  # Filter out impossible values given condition platform. This is admittedly
  # fragile to changes in our supported platforms. Fortunately, these platforms
  # don't change often. Refactor if we run into trouble.
  platform = condition.PLATFORM
  if attribute == Attr.TARGET and platform != '*' and platform != 'linux':
    values.difference_update(['ChromiumOS', 'ChromeOS'])
  if attribute == Attr.ARCHITECTURE and platform == 'win':
    values.intersection_update(['ia32', 'x64'])
  if attribute == Attr.ARCHITECTURE and platform == 'mac':
    values.intersection_update(['x64'])

  return values

def GenerateConditionExpansion(condition):
  """Expand wildcard in condition into all possible matching conditions."""
  architectures = GetAttributeValuesRange(Attr.ARCHITECTURE, condition)
  targets = GetAttributeValuesRange(Attr.TARGET, condition)
  platforms = GetAttributeValuesRange(Attr.PLATFORM, condition)
  return set(SourceListCondition(arch, target, plat)
                for (arch, target, plat)
                in itertools.product(architectures, targets, platforms))

def ReduceConditionalLogic(source_set):
  """Reduces the conditions for the given SourceSet.

  The reduction leverages what we know about the space of possible combinations,
  finding cases where conditions span all values possible of a given attribute.
  In such cases, these conditions can be flattened into a single condition with
  the spanned attribute removed.

  There is room for further reduction (e.g. Quine-McCluskey), not implemented
  at this time."""

  ConditionReduction = collections.namedtuple('ConditionReduction',
                                              'condition, matches')
  reduced_conditions = set()

  for condition in source_set.conditions:
    condition_dict = condition._asdict()

    for attribute in Attr:
      # Set attribute value to wildcard and find matching attributes.
      original_attribute_value = condition_dict[attribute]
      condition_dict[attribute] = '*'
      new_condition = SourceListCondition(**condition_dict)

      # Conditions with wildcards can replace existing conditions iff the
      # source set contains conditions covering all possible expansions
      # of the wildcarded values.
      matches = GetAllMatchingConditions(source_set.conditions, new_condition)
      if matches == GenerateConditionExpansion(new_condition):
        reduced_conditions.add(ConditionReduction(new_condition,
                                                  frozenset(matches)))
      else:
        # This wildcard won't work, restore the original value.
        condition_dict[attribute] = original_attribute_value

  # Finally, find the most efficient reductions. Do a pairwise comparison of all
  # reductions to de-dup and remove those that are covered by more inclusive
  # conditions.
  did_work = True
  while did_work:
    did_work = False
    for reduction_pair in itertools.combinations(reduced_conditions, 2):
      if reduction_pair[0].matches.issubset(reduction_pair[1].matches):
        reduced_conditions.remove(reduction_pair[0])
        did_work = True
        break
      elif reduction_pair[1].matches.issubset(reduction_pair[0].matches):
        reduced_conditions.remove(reduction_pair[1])
        did_work = True
        break

  # Apply the reductions to the source_set.
  for reduction in reduced_conditions:
    source_set.conditions.difference_update(reduction.matches)
    source_set.conditions.add(reduction.condition)


def ParseOptions():
  """Parses the options and terminates program if they are not sane.

  Returns:
    The pair (optparse.OptionValues, [string]), that is the output of
    a successful call to parser.parse_args().
  """
  parser = optparse.OptionParser(
      usage='usage: %prog [options] DIR')

  parser.add_option('-s',
                    '--source_dir',
                    dest='source_dir',
                    default='.',
                    metavar='DIR',
                    help='FFmpeg source directory.')

  parser.add_option('-b',
                    '--build_dir',
                    dest='build_dir',
                    default='.',
                    metavar='DIR',
                    help='Build root containing build.x64.linux, etc...')

  parser.add_option('-p',
                    '--print_licenses',
                    dest='print_licenses',
                    default=False,
                    action='store_true',
                    help='Print all licenses to console.')

  options, args = parser.parse_args()

  if not options.source_dir:
    parser.error('No FFmpeg source directory specified')
  elif not os.path.exists(options.source_dir):
    parser.error('FFmpeg source directory does not exist')

  if not options.build_dir:
    parser.error('No build root directory specified')
  elif not os.path.exists(options.build_dir):
    parser.error('FFmpeg build directory does not exist')

  return options, args


def WriteGn(fd, disjoint_sets):
  fd.write(COPYRIGHT)
  fd.write(GN_HEADER)

  # Generate conditional stanza for each disjoint source set.
  for s in reversed(disjoint_sets):
    fd.write(s.GenerateGnStanza())


# Lists of files that are exempt from searching in GetIncludedSources.
IGNORED_INCLUDE_FILES = [
    # Chromium generated files
    'config.h',
    os.path.join('libavutil', 'avconfig.h'),
    os.path.join('libavutil', 'ffversion.h'),

    # Current configure values are set such that we don't include these (because
    # of various defines) and we also don't generate them at all, so we will
    # fail to find these because they don't exist in our repository.
    os.path.join('libavcodec', 'aacps_tables.h'),
    os.path.join('libavcodec', 'aacps_fixed_tables.h'),
    os.path.join('libavcodec', 'aacsbr_tables.h'),
    os.path.join('libavcodec', 'aac_tables.h'),
    os.path.join('libavcodec', 'cabac_tables.h'),
    os.path.join('libavcodec', 'cbrt_tables.h'),
    os.path.join('libavcodec', 'cbrt_fixed_tables.h'),
    os.path.join('libavcodec', 'mpegaudio_tables.h'),
    os.path.join('libavcodec', 'pcm_tables.h'),
    os.path.join('libavcodec', 'sinewin_tables.h'),
    os.path.join('libavcodec', 'sinewin_fixed_tables.h'),
]


# Known licenses that are acceptable for static linking
# DO NOT ADD TO THIS LIST without first confirming with lawyers that the
# licenses are okay to add.
LICENSE_WHITELIST = [
    'BSD (3 clause) LGPL (v2.1 or later)',
    'BSL (v1) LGPL (v2.1 or later)',
    'ISC GENERATED FILE',
    'LGPL (v2.1 or later)',
    'LGPL (v2.1 or later) GENERATED FILE',
    'MIT/X11 (BSD like)',
    'Public domain LGPL (v2.1 or later)',
]


# Files permitted to report an UNKNOWN license. All files mentioned here should
# give the full path from the source_dir to avoid ambiguity.
# DO NOT ADD TO THIS LIST without first confirming with lawyers that the files
# you're adding have acceptable licenses.
UNKNOWN_WHITELIST = [
    # From of Independent JPEG group. No named license, but usage is allowed.
    os.path.join('libavcodec', 'jrevdct.c'),
    os.path.join('libavcodec', 'jfdctfst.c'),
    os.path.join('libavcodec', 'jfdctint_template.c'),
]


# Regex to find lines matching #include "some_dir\some_file.h".
INCLUDE_REGEX = re.compile('#\s*include\s+"([^"]+)"')

# Regex to find whacky includes that we might be overlooking (e.g. using macros
# or defines).
EXOTIC_INCLUDE_REGEX = re.compile('#\s*include\s+[^"<\s].+')

# Prefix added to renamed files as part of
RENAME_PREFIX = 'autorename'

# Match an absolute path to a generated auotorename_ file.
RENAME_REGEX = re.compile('.*' + RENAME_PREFIX + '_.+');

# Content for the rename file. #includes the original file to ensure the two
# files stay in sync.
RENAME_CONTENT = """// File automatically generated. See crbug.com/495833.
{0}include "{1}"
"""

def GetIncludedSources(file_path, source_dir, include_set):
  """Recurse over include tree, accumulating absolute paths to all included
  files (including the seed file) in include_set.

  Pass in the set returned from previous calls to avoid re-walking parts of the
  tree. Given file_path may be relative (to options.src_dir) or absolute.

  NOTE: This algorithm is greedy. It does not know which includes may be
  excluded due to compile-time defines, so it considers any mentioned include.

  NOTE: This algorithm makes hard assumptions about the include search paths.
  Paths are checked in the order:
  1. Directory of the file containing the #include directive
  2. Directory specified by source_dir

  NOTE: Files listed in IGNORED_INCLUDE_FILES will be ignored if not found. See
  reasons at definition for IGNORED_INCLUDE_FILES.
  """
  # Use options.source_dir to correctly resolve relative file path. Use only
  # absolute paths in the set to avoid same-name-errors.
  if not os.path.isabs(file_path):
    file_path = os.path.abspath(os.path.join(source_dir, file_path))

  current_dir = os.path.dirname(file_path)

  # Already processed this file, bail out.
  if file_path in include_set:
    return include_set

  include_set.add(file_path)

  for line in open(file_path):
    include_match = INCLUDE_REGEX.search(line)

    if not include_match:
      if EXOTIC_INCLUDE_REGEX.search(line):
        print 'WARNING: Investigate whacky include line:', line
      continue

    include_file_path = include_match.group(1)

    # These may or may not be where the file lives. Just storing temps here
    # and we'll checking their validity below.
    include_path_in_current_dir = os.path.join(current_dir, include_file_path)
    include_path_in_source_dir = os.path.join(source_dir, include_file_path)
    resolved_include_path = ''

    # Check if file is in current directory.
    if os.path.isfile(include_path_in_current_dir):
      resolved_include_path = include_path_in_current_dir
    # Else, check source_dir (should be FFmpeg root).
    elif os.path.isfile(include_path_in_source_dir):
      resolved_include_path = include_path_in_source_dir
    # Else, we couldn't find it :(.
    elif include_file_path in IGNORED_INCLUDE_FILES:
      continue
    else:
      exit('Failed to find file ' + include_file_path)

    # At this point we've found the file. Check if its in our ignore list which
    # means that the list should be updated to no longer mention this file.
    if include_file_path in IGNORED_INCLUDE_FILES:
      print('Found %s in IGNORED_INCLUDE_FILES. Consider updating the list '
            'to remove this file.' % str(include_file_path))

    GetIncludedSources(resolved_include_path, source_dir, include_set)


def CheckLicensesForSources(sources, source_dir, print_licenses):
  # Assumed to be two back from source_dir (e.g. third_party/ffmpeg/../..).
  source_root = os.path.abspath(
      os.path.join(source_dir, os.path.pardir, os.path.pardir))

  licensecheck_path = os.path.abspath(os.path.join(
      source_root, 'third_party', 'devscripts', 'licensecheck.pl'))
  if not os.path.exists(licensecheck_path):
    exit('Could not find licensecheck.pl: ' + str(licensecheck_path))

  check_process = subprocess.Popen(
      [licensecheck_path, '-m', '-l', '100']
      + [os.path.abspath(s) for s in sources], stdout=subprocess.PIPE,
      stderr=subprocess.PIPE)
  stdout, _ = check_process.communicate()

  # Get the filename and license out of the stdout. stdout is expected to be
  # "/abspath/to/file: *No copyright* SOME LICENSE".
  for line in stdout.strip().splitlines():
    filename, licensename = line.split('\t', 1)
    licensename = licensename.replace('*No copyright*', '').strip()
    rel_file_path = os.path.relpath(filename, os.path.abspath(source_dir))

    if (licensename in LICENSE_WHITELIST or
        (licensename == 'UNKNOWN' and rel_file_path in UNKNOWN_WHITELIST)):
      if print_licenses:
        print filename, ':', licensename
      continue

    print 'UNEXPECTED LICENSE: %s: %s' % (filename, licensename)
    return False

  return True


def CheckLicensesForStaticLinking(sources_to_check, source_dir, print_licenses):
  print 'Checking licenses...'
  return CheckLicensesForSources(sources_to_check, source_dir, print_licenses)


def FixBasenameCollision(old_path, new_path, content):
  with open(new_path, "w") as new_file:
    new_file.write(content)


def FixObjectBasenameCollisions(disjoint_sets, all_sources, do_rename_cb,
                                log_renames = True):
  """Mac libtool warns needlessly when it encounters two object files with
  the same basename in a given static library. See more at
  https://code.google.com/p/gyp/issues/detail?id=384#c7

  Here we hack around the issue by making a new source file with a different
  base name, and #including the original file.

  If upstream changes the name such that the collision no longer exists, we
  detect the presence of a renamed file in all_sources which is overridden and
  warn that it should be removed."""

  SourceRename = collections.namedtuple('SourceRename', 'old_path, new_path')
  known_basenames = set()
  all_renames = set()

  for source_set in disjoint_sets:
    # Track needed adjustments to change when we're done with each SourceSet.
    renames = set()

    for source_path in source_set.sources:
      folder, filename = os.path.split(source_path)
      basename, _ = os.path.splitext(filename)

      # Sanity check: source set should not have any renames prior to this step.
      if RENAME_PREFIX in basename:
        exit('Found unexpected renamed file in SourceSet: %s' % source_path)

      # Craft a new unique basename from the path of the colliding file
      if basename in known_basenames:
        name_parts = source_path.split(os.sep)
        name_parts.insert(0, RENAME_PREFIX)
        new_filename = '_'.join(name_parts)
        new_source_path = (new_filename if folder == ''
                           else os.sep.join([folder, new_filename]))

        renames.add(SourceRename(source_path, new_source_path))
      else:
        known_basenames.add(basename)

    for rename in renames:
      if log_renames:
        print 'Fixing basename collision: %s -> %s' % (rename.old_path,
                                                       rename.new_path)
      _, old_filename = os.path.split(rename.old_path)
      _, file_extension = os.path.splitext(old_filename)
      include_prefix = '%' if (file_extension == '.asm') else '#'

      do_rename_cb(rename.old_path, rename.new_path,
                   RENAME_CONTENT.format(include_prefix, old_filename))

      source_set.sources.remove(rename.old_path)
      source_set.sources.add(rename.new_path)
      all_renames.add(rename.new_path)

  # Now, with all collisions handled, walk the set of known sources and warn
  # about any renames that were not replaced. This should indicate that an old
  # collision is now resolved by some external/upstream change.
  for source_path in all_sources:
    if RENAME_PREFIX in source_path and source_path not in all_renames:
      print 'WARNING: %s no longer collides. DELETE ME!' % source_path


def UpdateCredits(sources_to_check, source_dir):
  print 'Updating ffmpeg credits...'
  updater = credits_updater.CreditsUpdater(source_dir)
  for source_name in sources_to_check:
    updater.ProcessFile(source_name)
  updater.PrintStats()
  updater.WriteCredits()


def main():
  options, _ = ParseOptions()

  # Generate map of FFmpeg source files.
  source_dir = options.source_dir
  source_files = GetSourceFiles(source_dir)
  object_to_sources = GetObjectToSourceMapping(source_files)

  sets = []

  for arch in SUPPORT_MATRIX[Attr.ARCHITECTURE]:
    for target in SUPPORT_MATRIX[Attr.TARGET]:
      for platform in SUPPORT_MATRIX[Attr.PLATFORM]:
        # Assume build directory is of the form build.$arch.$platform/$target.
        name = ''.join(['build.', arch, '.', platform])
        build_dir = os.path.join(options.build_dir, name, target)
        if not os.path.exists(build_dir):
          continue
        print 'Processing build directory: %s' % name

        object_files = GetObjectFiles(build_dir)

        # Generate the set of source files to build said target.
        s = GetSourceFileSet(object_to_sources, object_files)
        sets.append(SourceSet(s, set([SourceListCondition(arch, target,
                                                          platform)])))

  sets = CreatePairwiseDisjointSets(sets)

  for source_set in sets:
    ReduceConditionalLogic(source_set)

  if not sets:
    exit('ERROR: failed to find any source sets. ' +
         'Are build_dir (%s) and/or source_dir (%s) options correct?' %
         (options.build_dir, options.source_dir))

  FixObjectBasenameCollisions(sets, source_files, FixBasenameCollision)

  # Build up set of all sources and includes.
  sources_to_check = set()
  for source_set in sets:
    for source in source_set.sources:
      GetIncludedSources(source, source_dir, sources_to_check)

  # Remove autorename_ files now that we've grabbed their underlying includes.
  # We generated autorename_ files above and should not consider them for
  # licensing or credits.
  sources_to_check = filter(lambda s: not RENAME_REGEX.search(s),
                            sources_to_check)

  if not CheckLicensesForStaticLinking(sources_to_check, source_dir,
                                       options.print_licenses):
    exit('GENERATE FAILED: invalid licenses detected.')
  print 'License checks passed.'
  UpdateCredits(sources_to_check, source_dir)

  gn_file_name = os.path.join(options.source_dir, 'ffmpeg_generated.gni')
  print 'Writing:', gn_file_name
  with open(gn_file_name, 'w') as fd:
    WriteGn(fd, sets)


if __name__ == '__main__':
  main()
