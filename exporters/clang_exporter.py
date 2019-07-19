#!/usr/bin/env python2.7

"""
CLang export for Pigaios.

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

import json
from threading import current_thread

import clang.cindex
from clang.cindex import Diagnostic, CursorKind, TokenKind
import re

from base_support import *
from SimpleEval import simple_eval
from simple_macro_parser import CMacroExtractor

#-------------------------------------------------------------------------------
CONDITIONAL_OPERATORS = ["==", "!=", "<", ">", ">=", "<=", "?"]
INLINE_NAMES = ["inline", "__inline", "__inline__", "__forceinline", "always_inline"]

SCAN_ELEMENTS = [CursorKind.FUNCTION_DECL, CursorKind.FUNCTION_TEMPLATE,
                 CursorKind.CXX_METHOD, CursorKind.CONSTRUCTOR,
                 CursorKind.DESTRUCTOR, CursorKind.OBJC_INSTANCE_METHOD_DECL]

#-----------------------------------------------------------------------------
def basename(path):
  pos1 = path[::-1].find("\\")
  pos2 = path[::-1].find("/")

  if pos1 == -1: pos1 = len(path)
  if pos2 == -1: pos2 = len(path)
  pos = min(pos1, pos2)

  return path[len(path)-pos:]

#-------------------------------------------------------------------------------
def severity2text(severity):
  if severity == Diagnostic.Ignored:
    return ""
  elif severity == Diagnostic.Note:
    return "note"
  elif severity == Diagnostic.Warning:
    return "warning"
  elif severity == Diagnostic.Error:
    return "error"
  elif severity == Diagnostic.Fatal:
    return "fatal"
  else:
    return "unknown"

#-------------------------------------------------------------------------------
def is_inline(cursor):
  if cursor.kind != CursorKind.FUNCTION_DECL:
    return False

  for token in cursor.get_tokens():
    tkn = token.spelling
    for name in INLINE_NAMES:
      if tkn.find(name) > -1:
        return True
    if tkn == "{":
      break

  return False

#-------------------------------------------------------------------------------
def is_static(cursor):
  if cursor.kind != CursorKind.FUNCTION_DECL:
    return False
  token = next(cursor.get_tokens(), None)
  if token is None:
    return False
  return token.spelling == "static"

#-------------------------------------------------------------------------------
def dump_ast(cursor, level = 0):
  token = next(cursor.get_tokens(), None)
  if token is not None:
    token = token.spelling

  print("  "*level, cursor.kind, repr(cursor.spelling), repr(token), cursor.type.spelling, cursor.location)
  for children in cursor.get_children():
    dump_ast(children, level+1)

#-------------------------------------------------------------------------------
def json_dump(x):
  return json.dumps(x, ensure_ascii=False)

#-------------------------------------------------------------------------------
class CCLangVisitor:
  def __init__(self, name):
    self.conditions = 0
    self.name = name
    self.loops = 0
    self.enums = {}
    self.calls = {}
    self.switches = []
    self.constants = set()
    self.externals = set()
    self.indirects = []
    self.recursive = False
    self.globals_uses = set()

    self.local_vars = set()
    self.global_variables = set()

    self.mul = False
    self.div = False

  def __str__(self):
    msg = "<Function %s: conditions %d, loops %d, calls %s, switches %s, externals %s, constants %s>" 
    return msg % (self.name, self.conditions, self.loops, self.calls,
                  self.switches, self.externals, self.constants)

  def __repr__(self):
    return self.__str__()

  def visit_LITERAL(self, cursor):
    # TODO:XXX:FIXME: It seems that the value of some (integer?) literals with
    # macros cannot be properly resolved as spelling returns '' and get_tokens()
    # will return the textual representation in the source code file before the
    # preprocessor is run. Well, CLang...

    #print("Visiting LITERAL", cursor.spelling)
    for token in cursor.get_tokens():
      if token.kind != TokenKind.LITERAL:
        continue

      tmp = token.spelling
      if cursor.kind == CursorKind.FLOATING_LITERAL:
        if tmp.endswith("f"):
          tmp = tmp.strip("f")
      elif cursor.kind == CursorKind.STRING_LITERAL or tmp.find('"') > -1 or tmp.find("'") > -1:
        if tmp.startswith('"') and tmp.endswith('"'):
          tmp = get_printable_value(tmp.strip('"'))
          self.externals.add(tmp)

        self.constants.add(tmp)
        continue

      try:
        result = simple_eval(tmp)
      except:
        pass

      break

  def visit_ENUM_DECL(self, cursor):
    #print("Visiting ENUM DECL")
    value = 0
    for children in cursor.get_children():
      tokens = list(children.get_tokens())
      if len(tokens) == 0:
        # XXX:FIXME: I'm ignoring it too fast, I should take a careful look into
        # it to be sure what should I do here...
        break

      name = tokens[0].spelling
      if len(tokens) == 3:
        value = get_clean_number(tokens[2].spelling)

      # Some error parsing partial source code were an enum member has been
      # initialized to a macro that we know nothing about...
      if type(value) is str:
        return True

      self.enums[name] = value
      if len(tokens) == 1:
        value += 1

    return True

  def visit_IF_STMT(self, cursor):
    #print("Visiting IF_STMT")
    # Perform some (fortunately) not too complex parsing of the IF_STMT as the
    # Clang Python bindings always lack support for everything half serious one
    # needs to do...
    par_level = 0
    tmp_conds = 0
    at_least_one_parenthesis = False
    for token in cursor.get_tokens():
      clean_token = str(token.spelling)
      if clean_token == "(":
        # The first time we find a parenthesis we can consider there is at least
        # one condition.
        if not at_least_one_parenthesis:
          tmp_conds += 1

        at_least_one_parenthesis = True
        par_level += 1
      elif clean_token == ")":
        par_level -= 1
        # After we found at least one '(' and the level of parenthesis is zero,
        # we finished with the conditional part of the IF_STMT
        if par_level == 0 and at_least_one_parenthesis:
          break
      # If there are 2 or more conditions, these operators will be required
      elif clean_token in ["||", "&&"]:
        tmp_conds += 1

    self.conditions += tmp_conds

  def visit_CALL_EXPR(self, cursor):
    #print("Visiting CALL_EXPR")
    if cursor.spelling == self.name:
      self.recursive = True

    token = next(cursor.get_tokens(), None)
    if token is not None:
      token = token.spelling
      if token != "" and token is not None:
        if token != cursor.spelling:
          self.indirects.append(cursor.spelling)

    spelling = cursor.spelling
    try:
      self.calls[cursor.spelling] += 1
    except:
      self.calls[cursor.spelling] = 1

  def visit_loop(self, cursor):
    #print("Visiting LOOP")
    self.loops += 1

  def visit_WHILE_STMT(self, cursor):
    self.visit_loop(cursor)

  def visit_FOR_STMT(self, cursor):
    self.visit_loop(cursor)

  def visit_DO_STMT(self, cursor):
    self.visit_loop(cursor)

  def visit_SWITCH_STMT(self, cursor):
    #print("Visiting SWITCH_STMT")
    # As always, the easiest way to get the cases and values from a SWITCH_STMT
    # using the CLang Python bindings is by parsing the tokens...
    cases = set()
    next_case = False
    default = 0
    for token in cursor.get_tokens():
      if token.kind not in [TokenKind.KEYWORD, TokenKind.LITERAL]:
        continue

      if token.kind == TokenKind.KEYWORD:
        clean_token = str(token.spelling)
        # The next token will be the case value
        if clean_token == "case":
          next_case = True
          continue
        # Do not do anything special with default cases, other than recording it
        elif clean_token == "default":
          default = 1
          continue

      if next_case:
        next_case = False
        # We use a set() for the cases to "automagically" order them
        cases.add(clean_token)

    self.switches.append([len(cases) + default, list(cases)])

  def visit_BINARY_OPERATOR(self, cursor):
    for token in cursor.get_tokens():
      if token.kind == TokenKind.PUNCTUATION:
        if token.spelling == "*":
          self.mul = True
        elif token.spelling == "/":
          self.div = True
        elif token.spelling in CONDITIONAL_OPERATORS:
          self.conditions += 1

  def visit_PARM_DECL(self, cursor):
    self.local_vars.add(cursor.spelling)

  def visit_VAR_DECL(self, cursor):
    self.local_vars.add(cursor.spelling)
  
  def visit_DECL_REF_EXPR(self, cursor):
    name = cursor.spelling
    if name not in self.local_vars:
      if name in self.global_variables:
        self.globals_uses.add(name)

#-------------------------------------------------------------------------------
class CLangParser:
  def __init__(self):
    self.index = None
    self.tu = None
    self.diags = None
    self.source_path = None
    self.warnings = 0
    self.errors = 0
    self.fatals = 0
    self.total_elements = 0

  def parse(self, src, args):
    self.source_path = src
    self.index = clang.cindex.Index.create()
    self.tu = self.index.parse(path=src, args=args)
    self.diags = self.tu.diagnostics
    for diag in self.diags:
      if diag.severity == Diagnostic.Warning:
        self.warnings += 1
      elif diag.severity == Diagnostic.Error:
        self.errors += 1
      elif diag.severity == Diagnostic.Fatal:
        self.fatals += 1

      export_log("%s:%d,%d: %s: %s" % (diag.location.file, diag.location.line,
              diag.location.column, severity2text(diag.severity), diag.spelling))

  def parse_buffer(self, src, buf, args):
    self.source_path = src
    self.index = clang.cindex.Index.create()
    self.tu = self.index.parse(path=src, args=args, unsaved_files=[(src, buf)])
    self.diags = self.tu.diagnostics
    for diag in self.diags:
      if diag.severity == Diagnostic.Warning:
        self.warnings += 1
      elif diag.severity == Diagnostic.Error:
        self.errors += 1
      elif diag.severity == Diagnostic.Fatal:
        self.fatals += 1

      export_log("%s:%d,%d: %s: %s" % (diag.location.file, diag.location.line,
              diag.location.column, severity2text(diag.severity), diag.spelling))

  def visitor(self, obj, cursor=None):
    if cursor is None:
      cursor = self.tu.cursor

    for children in cursor.get_children():
      self.total_elements += 1

      # Check if a visit_EXPR_TYPE member exists in the given object and call it
      # passing the current children element.
      kind_name = str(children.kind)
      element = kind_name[kind_name.find(".")+1:]
      method_name = 'visit_%s' % element
      if method_name in dir(obj):
        func = getattr(obj, method_name)
        if func(children):
          continue

      # Same as before but we pass to the member any literal expression.
      method_name = 'visit_LITERAL'
      if children.kind >= CursorKind.INTEGER_LITERAL and \
           children.kind <= CursorKind.STRING_LITERAL:
        if method_name in dir(obj):
          func = getattr(obj, method_name)
          if func(children):
            continue

      self.visitor(obj, cursor=children)

#-------------------------------------------------------------------------------
class CClangExporter(CBaseExporter):
  def __init__(self, cfg_file):
    CBaseExporter.__init__(self, cfg_file)
    self.source_cache = {}
    self.global_variables = set()

    self.header_files = []
    self.src_definitions = []

  def get_function_source(self, cursor):
    start_line = cursor.extent.start.line
    end_line   = cursor.extent.end.line

    start_loc = cursor.location
    filename = start_loc.file.name
    if filename not in self.source_cache:
      self.source_cache[filename] = open(filename, "rb").readlines()

    source = "".join(self.source_cache[filename][start_line-1:end_line])
    return source

  def get_prototype(self, cursor):
    args = []
    for arg in cursor.get_arguments():
      args.append("%s %s" % (arg.type.spelling, arg.spelling))

    prototype = None
    definition = cursor.get_definition()
    if definition is not None:
      prototype = "%s %s(%s)" % (cursor.get_definition().result_type.spelling, cursor.spelling, ", ".join(args))

    return prototype

  def strip_macros(self, filename):
    ret = []
    for line in open(filename, "rb").readlines():
      line = line.strip("\r").strip("\n")
      if line.find("#include") == -1 and line.strip(" ").strip("\t").strip(" ").startswith("#"):
        ret.append("// stripped: %s" % line)
        continue
      ret.append(line)
    return "\n".join(ret)

  def element2kind(self, element):
    if element.kind == CursorKind.STRUCT_DECL:
      return "struct"
    elif element.kind == CursorKind.ENUM_DECL:
      return "enum"
    elif element.kind == CursorKind.UNION_DECL:
      return "union"
    elif element.kind == CursorKind.TYPEDEF_DECL:
      return "typedef"
    elif element.kind == CursorKind.ENUM_CONSTANT_DECL:
      return ""
    else:
      return "" # Unknown thing

  def clean_name(self, name):
    return "".join([c for c in name if c.isalpha() or c.isdigit() or c == "_"]).rstrip()

  def parse_bitfield(self, field):
    elem_name = field.spelling
    type_name = field.type.spelling
    bit_size = list(list(field.get_children())[0].get_tokens())[0].spelling
    return type_name, elem_name, bit_size

  def is_array(self, field):
    type_name = field.type.spelling
    if re.findall(r'\[[A-Za-z0-9]*\]', type_name):
      return True
    else:
      return False

  def is_primitive_field(self, field):
    # For such cases as PACKED_ATTR, UNEXPOSED_ATTR and etc
    if not field.kind == CursorKind.FIELD_DECL:
      return False

    children = list(field.get_children())
    if children:
      kinds = set(map(lambda x: x.kind, children))
      if kinds & set((CursorKind.STRUCT_DECL, CursorKind.UNION_DECL)):
        return False

    return True

  def is_struct(self, field):
    if field.kind == CursorKind.STRUCT_DECL:
      return True

    children = list(field.get_children())
    if children:
      if children[0].kind == CursorKind.STRUCT_DECL:
        return True

    return False

  def is_union(self, field):
    if field.kind == CursorKind.UNION_DECL:
      return True

    children = list(field.get_children())
    if children:
      if children[0].kind == CursorKind.UNION_DECL:
        return True

    return False

  def parse_field(self, field):
    field_name = field.spelling
    type_name = field.type.spelling

    if self.is_primitive_field(field):

      if field.kind == CursorKind.PACKED_ATTR:
        return None

      if field.is_bitfield():
        bit_length = list(list(field.get_children())[0].get_tokens())[0].spelling
        field_src = "%s %s: %s;" % (type_name, field_name, bit_length)
        return field_name, field_src

      elif self.is_array(field):
        pos = type_name.find('[')
        field_src = type_name[:pos] + field_name + type_name[pos:] + ";"
        return field_name, field_src

      elif re.findall(r'\(\*+\)', type_name):
        pos = type_name.find(')(')
        field_src = (type_name[:pos] + " %s " + type_name[pos:] + ";") % field_name
        return field_name, field_src

      else:
        field_src = "%s %s;" % (type_name, field_name)
        return field_name, field_src


    elif self.is_struct(field):
        if field.kind == CursorKind.FIELD_DECL:
          struct = list(field.get_children())[0]
          struct_name, struct_src = self.parse_struct(struct, is_nested=True)
        else:
          struct = self.parse_struct(field, is_nested=False)
          if not struct:
            return None
          struct_name, struct_src = struct

        if self.is_array(field):
          pos = type_name.find('[')
          struct_src = "%s %s%s;" % (struct_src, field.spelling, type_name[pos:])
        else:
          struct_src = "%s %s;" % (struct_src, field.spelling)
        return field.spelling, struct_src
    elif self.is_union(field):
        if field.kind == CursorKind.FIELD_DECL:
          union = list(field.get_children())[0]
          union_name, union_src = self.parse_union(union, is_nested=True)
        else:
          union = self.parse_union(field, is_nested=True)
          if not union:
            return None
          union_name, union_src = union

        if self.is_array(field):
          pos = type_name.find('[')
          union_src = "%s %s%s;" % (union_src, field.spelling, type_name[pos:])
        else:
         union_src = "%s %s;" % (union_src, field.spelling)
        return field.spelling, union_src
    else:
      return None



  def parse_union(self, union, is_nested=False):
    is_anon = ("(anonymous " in union.type.spelling) or (union.spelling == "")
    if is_anon and not is_nested:
      return None

    union_name = union.spelling
    union_src = ["union %s" % union_name, "{"]

    for field in union.get_children():
      field_pair = self.parse_field(field)
      if not field_pair:
        continue
      field_name, field_src = field_pair
      union_src.append(field_src)

    if is_anon:
      union_src.append("}")
    else:
      union_src.append("};")

    union_src = '\n'.join(union_src)
    return union_name, union_src

  def parse_struct(self, struct, is_nested=False):
    is_anon = ("(anonymous " in struct.type.spelling) or (struct.spelling =="");
    if is_anon and not is_nested:
      return None

    struct_name = struct.spelling
    struct_src = ["struct %s" % struct_name, "{"]

    for field in struct.get_children():
      field_pair = self.parse_field(field)
      if not field_pair:
        continue
      field_name, field_src = field_pair
      struct_src.append(field_src)

    if is_anon:
      struct_src.append("}")
    else:
      struct_src.append("};")

    struct_src = '\n'.join(struct_src)
    return struct_name, struct_src

  def parse_typedef(self, typedef):
    typedef_name = typedef.spelling
    underlying_typename = typedef.underlying_typedef_type.spelling

    if '(anonymous struct' in underlying_typename or underlying_typename.startswith('struct'):
      child = list(typedef.get_children())[0]
      if child.kind == CursorKind.STRUCT_DECL and child.spelling == '':
        struct = child
        struct_name, struct_src = self.parse_struct(struct, is_nested=True)
        typedef_src = "typedef %s %s;" % (struct_src, typedef.spelling)
      elif child.kind == CursorKind.TYPE_REF:
        typedef_src = "typedef %s %s;" % (underlying_typename, typedef.spelling)
      else:
        return None
    elif '(anonymous union' in underlying_typename or underlying_typename.startswith('union'):
      child = list(typedef.get_children())[0]
      if child.kind == CursorKind.UNION_DECL and child.spelling == '':
        union = child
        union_name, union_src = self.parse_union(union, is_nested=True)
        typedef_src = "typedef %s %s;" % (union_src, typedef.spelling)
      elif child.kind == CursorKind.TYPE_REF:
        typedef_src = "typedef %s %s;" % (underlying_typename, typedef.spelling)
      else:
        return None
    else:
      # For case "typedef int (*bar)(int *, void*);"
      if re.findall(r'\(\*+\)', underlying_typename):
        pos = underlying_typename.find(')(')
        src = ("typedef " + underlying_typename[:pos] + " %s " + underlying_typename[pos:] + ";") % typedef_name
        return typedef_name, src

      # For case "typedef int foo(int *, void *);"
      pos = underlying_typename.find('(')
      if pos > -1:
        src = ("typedef " + underlying_typename[:pos-1] + " %s " + underlying_typename[pos:] + ";") % typedef_name
        return typedef_name, src

      typedef_src = "typedef %s %s;" % (underlying_typename, typedef.spelling)


    return typedef_name, typedef_src

  def get_enum_constant_value(self, element):
    tokens = map(lambda x: x.spelling, list(element.get_tokens()))
    return ' '.join(tokens)

  def parse_enum(self, enum):
    enum_name = enum.spelling

    ret = ["enum " + enum_name + " {"]

    for enum_constant in enum.get_children():
      label = enum_constant.spelling
      children = list(enum_constant.get_children())
      if not children:
        ret.append("%s," % label)
        continue

      value_cursor = children[0]
      value = self.get_enum_constant_value(value_cursor)
      ret.append("%s = %s," % (label, value))

    ret.append("};")
    enum_src = '\n'.join(ret)
    return enum_name, enum_src

  def export_one(self, filename, args, is_c):
    parser = CLangParser()
    parser.parse(filename, args)
    self.warnings += parser.warnings
    self.errors += parser.errors
    self.fatals += parser.fatals

    things = 0
    if parser.fatals > 0 or parser.errors > 0:
      for element in parser.tu.cursor.get_children():
        fileobj = element.location.file
        if fileobj is not None and fileobj.name != filename:
          continue        
        things += 1

      if things == 0:
        # We haven't discovered a single thing and errors happened parsing the
        # file, let's try again but stripping macros this time...
        new_src = self.strip_macros(filename)
        parser = CLangParser()
        parser.parse_buffer(filename, new_src, args)
        self.warnings += parser.warnings
        self.errors += parser.errors
        self.fatals += parser.fatals

    db = self.get_db()
    cwd = os.getcwd()
    with db as cur:
      if not self.parallel:
        cur.execute("PRAGMA synchronous = OFF")
        cur.execute("BEGIN transaction")

      # Extract macros and magically group and convert them to enums.
      extractor = CMacroExtractor()
      enums = extractor.extract(filename)
      for name in enums:
        if name != "" and len(enums[name]) > 0:
          self.src_definitions.append(["enum", name, enums[name]])

      dones = set()
      for element in parser.tu.cursor.get_children():
        fileobj = element.location.file
        if fileobj is not None:
          pathname = os.path.realpath(os.path.dirname(fileobj.name))
          if not pathname.startswith(cwd):
            continue

          if fileobj.name not in self.header_files:
            if fileobj.name not in dones:
              dones.add(fileobj.name)

            if element.kind == CursorKind.STRUCT_DECL:
              struct = self.parse_struct(element)

              if not struct:
                continue

              struct_name, struct_src = struct
              self.src_definitions.append(["struct", struct_name, struct_src])
            elif element.kind == CursorKind.UNION_DECL:
              union = self.parse_union(element)

              if not union:
                continue

              union_name, union_src = union
              self.src_definitions.append(["union", union_name, union_src])
            elif element.kind == CursorKind.ENUM_DECL:
              enum_name, enum_src = self.parse_enum(element)
              self.src_definitions.append(["enum", enum_name, enum_src])
            elif element.kind == CursorKind.TYPEDEF_DECL:
              typedef = self.parse_typedef(element)

              if not typedef:
                continue

              typedef_name, typedef_src = typedef
              self.src_definitions.append(["typedef", typedef_name, typedef_src])

          if fileobj.name != filename:
            continue

        if element.kind == CursorKind.VAR_DECL:
          name = element.spelling
          self.global_variables = name

        if element.kind in SCAN_ELEMENTS:
          static = element.is_static_method()
          tokens = element.get_tokens()
          token = next(tokens, None)
          if token is not None:
            if token.spelling == "extern":
              continue

          obj = CCLangVisitor(element.spelling)
          obj.global_variables = self.global_variables
          obj.is_inlined = is_inline(element)
          obj.is_static = is_static(element)
          parser.visitor(obj, cursor=element)

          prototype = self.get_prototype(element)
          prototype2 = ""
          source = self.get_function_source(element)
          if source is None or source == "":
            continue

          sql = """insert into functions(
                                 ea, name, prototype, prototype2, conditions,
                                 constants, constants_json, loops, switchs,
                                 switchs_json, calls, externals, filename,
                                 callees_json, source, recursive, indirect, globals,
                                 inlined, static, basename)
                               values
                                 ((select count(ea)+1 from functions),
                                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  ?, ?, ?, ?, ?, ?)"""
          args = (obj.name, prototype, prototype2, obj.conditions,
                  len(obj.constants), json_dump(list(obj.constants)),
                  obj.loops, len(obj.switches), json_dump(list(obj.switches)),
                  len(obj.calls.keys()), len(obj.externals),
                  filename, json_dump(obj.calls), source, obj.recursive,
                  len(obj.indirects), len(obj.globals_uses), obj.is_inlined,
                  obj.is_static, basename(filename).lower(), )
          self.insert_row(sql, args, cur)

      self.header_files += list(dones)
      if not self.parallel:
        cur.execute("COMMIT")
