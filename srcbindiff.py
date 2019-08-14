#!/usr/bin/env python2.7

"""
Pigaios, a tool for matching and diffing source codes directly against binaries.
Copyright (c) 2018, Joxean Koret

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

from __future__ import print_function

import os
import sys
import json
import subprocess
from collections import OrderedDict
import argparse
from argsgenerators import MakefileArgsGenerator
from argsgenerators import SimpleArgsGenerator


from exporters.base_support import is_source_file, is_header_file, is_export_header

try:
  from colorama import colorama_text, Style, init
  init()
  has_colorama = True
except:
  has_colorama = False

try:
  from exporters import clang_exporter
  has_clang = True
except ImportError:
  has_clang = False

#-------------------------------------------------------------------------------
SBD_BANNER = """Source To Binary Differ command line tool version 0.0.1
Copyright (c) 2018, Joxean Koret"""
SBD_PROJECT_COMMENT = "# Default Source-Binary-Differ project configuration"
DEFAULT_PROJECT_FILE = os.path.join('__pigaios__', 'sbd-project.json')

#-------------------------------------------------------------------------------
class CSBDProject:
  def __init__(self, build_system=None):
    self.analyze_headers = False
    self.build_system = build_system

  def resolve_clang_includes(self):
    cmd = 'echo | clang -E -Wp,-v -'
    proc = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    stdout = proc.stdout.read()

    stdout = stdout.decode()

    lines = stdout.split('\n')
    begin = False
    includes = []

    for l in lines:
      if l == '#include <...> search starts here:':
        begin = True
        continue

      if l == 'End of search list.':
        break

      if begin:
        includes.append(l.strip())

    return includes

  def create_project(self, path, project_file):
    if os.path.exists(project_file):
      answer = None
      while answer not in ('', 'y', 'n', 'yes', 'no'):
        answer = raw_input("Project file %s already exists. Rewrite ([y]/n)?" % repr(project_file))
      if answer == 'n':
        return False

    config = OrderedDict()

    # Add the CLang specific configuration section
    config['GENERAL'] = {
      'clang-includes': self.resolve_clang_includes(),
      'inlines': 0,
    }
    config['GENERAL'] = OrderedDict(sorted(config['GENERAL'].items(), key=lambda x: x[0]))

    # Add the project specific configuration section
    base_path = os.path.basename(path)
    config['PROJECT'] = {
      "cflags": " -xc",
      "cxxflags": "-xc++",
      "export-file": "%s.sqlite" % base_path,
      "export-header": "%s-exported.h" % base_path,
      "export-indent": "clang-format -i",
    }
    config['PROJECT'] = OrderedDict(sorted(config['PROJECT'].items(), key=lambda x: x[0]))

    # And now add all discovered source files
    if self.build_system == 'Makefile':
      mag = MakefileArgsGenerator(path)
      file_to_args = mag.generate()
    else:
      sag = SimpleArgsGenerator(path)
      file_to_args = sag.generate()

    config['FILES'] = file_to_args

    with open(project_file, 'w') as f:
      json.dump(config, f, indent=4)

    return True

#-------------------------------------------------------------------------------
class CSBDExporter:
  def __init__(self, cfg_file, parallel = False):
    self.cfg_file = cfg_file
    self.parallel = parallel

  def export(self, use_clang):
    exporter = None
    if not has_clang:
      raise Exception("Python CLang bindings aren't installed!")
    exporter = clang_exporter.CClangExporter(self.cfg_file)
    exporter.parallel = self.parallel

    try:
      if not self.parallel:
        exporter.export()
      else:
        exporter.export_parallel()
    except KeyboardInterrupt:
      print("Aborted.")
      return

    if exporter.warnings + exporter.errors + exporter.fatals > 0:
      msg = "\n%d warning(s), %d error(s), %d fatal error(s)"
      print(msg % (exporter.warnings, exporter.errors, exporter.fatals))


#-------------------------------------------------------------------------------
def main():
  analyze_headers = False

  parser = argparse.ArgumentParser(description=SBD_BANNER)
  parser.add_argument('-create', help='Create a project in the current directory and discover source files.',
                      action='store_true')
  parser.add_argument('-export', help='Export the current project to one SQLite database.', action='store_true')
  parser.add_argument('-project', help='Use <file> as the project filename.',
                      dest="project_file", default=DEFAULT_PROJECT_FILE)
  parser.add_argument('-clang', help="Use the Clang Python bindings' to parse the source files (default)",
                      action='store_true', dest='use_clang', default=True)
  parser.add_argument('--no-parallel', help='Do not parallelize the compilation process (faster for small code bases).',
                      action='store_true', dest="parallel", default=False)
  parser.add_argument('-p', '--profile-export', help='Execute the command and show profiling data.',
                      action='store_true', dest='profiling')
  parser.add_argument('--analyze-headers', help='Analyze also all the header files.', action='store_true')
  parser.add_argument('-test', help='Test for the availability of exporters', action='store_true')
  args = parser.parse_args()

  if args.create:
    sbd_project = CSBDProject()
    sbd_project.analyze_headers = analyze_headers
    if sbd_project.create_project(os.getcwd(), args.project_file):
      print("Project file %s created." % repr(args.project_file))
  elif args.export:
    exporter = CSBDExporter(args.project_file, args.parallel)
    exporter.export(args.use_clang)
  elif args.profiling:
    import cProfile
    profiler = cProfile.Profile()
    exporter = CSBDExporter(args.project_file, args.parallel)
    profiler.runcall(exporter.export, (args.use_clang,))
    profiler.print_stats(sort="time")


if __name__ == "__main__":
    main()
