"""The Power Puter is a powerful node that can compute and evaluate Python-like code safely allowing
for complex operations for primitives and workflow items for output. From string concatenation, to
math operations, list comprehension, and node value output.

Originally based off https://github.com/pythongosssss/ComfyUI-Custom-Scripts/blob/aac13aa7ce35b07d43633c3bbe654a38c00d74f5/py/math_expression.py
under an MIT License https://github.com/pythongosssss/ComfyUI-Custom-Scripts/blob/aac13aa7ce35b07d43633c3bbe654a38c00d74f5/LICENSE
"""

import math
import ast
import json
import random
import dataclasses
import re
import time
import operator as op

from typing import Any, Callable, Optional, Union

from .constants import get_category, get_name
from .utils import ByPassTypeTuple, FlexibleOptionalInputType, any_type, get_dict_value
from .log import log_node_error, log_node_warn

from .power_lora_loader import RgthreePowerLoraLoader


@dataclasses.dataclass(frozen=True) # Note, kw_only=True is only python 3.10+
class Function():
  """Function data.

  Attributes:
    name: The name of the function as called from the node.
    call: The callable (reference, lambda, etc), or a string if on _Puter instance.
    args: A tuple that represents the minimum and maximum number of args (or arg for no limit).
  """

  name: str
  call: Union[Callable, str]
  args: tuple[int, Optional[int]]


_FUNCTIONS = {
  fn.name: fn for fn in [
    Function(name="round", call=round, args=(1, 2)),
    Function(name="ceil", call=math.ceil, args=(1, 1)),
    Function(name="floor", call=math.floor, args=(1, 1)),
    Function(name="sqrt", call=math.sqrt, args=(1, 1)),
    Function(name="min", call=min, args=(2, None)),
    Function(name="max", call=max, args=(2, None)),
    Function(name="random_int", call=random.randint, args=(2, 2)),
    Function(name="random_choice", call=random.choice, args=(2, None)),
    Function(name="re", call=re.compile, args=(1, 1)),
    Function(name="len", call=len, args=(1, 1)),
    # Casts
    Function(name="int", call=int, args=(1, 1)),
    Function(name="float", call=float, args=(1, 1)),
    Function(name="str", call=str, args=(1, 1)),
    Function(name="bool", call=bool, args=(1, 1)),
    Function(name="list", call=list, args=(1, 1)),
    Function(name="tuple", call=tuple, args=(1, 1)),
    # Special
    Function(name="node", call='_get_node', args=(0, 1)),
    Function(name="nodes", call='_get_nodes', args=(0, 1)),
    Function(name="input_node", call='_get_input_node', args=(0, 1)),
    Function(name="dir", call=dir, args=(1, 1)),
    Function(name="type", call=type, args=(1, 1)),
    Function(name="print", call=print, args=(0, None)),
  ]
}

# Special functions by class type (called from the Attrs.)
_SPECIAL_FUNCTIONS = {
  RgthreePowerLoraLoader.NAME: {
    # Get a list of the enabled loras from a power lora loader.
    "loras": RgthreePowerLoraLoader.get_enabled_loras_from_prompt_node,
    "triggers": RgthreePowerLoraLoader.get_enabled_triggers_from_prompt_node,
  }
}

# Series of regex checks for usage of a non-deterministic function. Using these is fine, but means
# the output can't be cached because it's either random, or is associated with another node that is
# not connected to ours (like looking up a node in the prompt). Using these means downstream nodes
# would always be run; that is fine for something like a final JSON output, but less so for a prompt
# text.
_NON_DETERMINISTIC_FUNCTION_CHECKS = [r'(?<!input_)(nodes?)\(', r'(?<!\.)(random_(int|choice))\(',]

_OPERATORS = {
  ast.Add: op.add,
  ast.Sub: op.sub,
  ast.Mult: op.mul,
  ast.Div: op.truediv,
  ast.FloorDiv: op.floordiv,
  ast.Pow: op.pow,
  ast.BitXor: op.xor,
  ast.USub: op.neg,
  ast.Mod: op.mod,
  ast.BitAnd: op.and_,
  ast.BitOr: op.or_,
  ast.Invert: op.invert,
  ast.And: lambda a, b: 1 if a and b else 0,
  ast.Or: lambda a, b: 1 if a or b else 0,
  ast.Not: lambda a: 0 if a else 1,
  ast.RShift: op.rshift,
  ast.LShift: op.lshift
}

_NODE_NAME = get_name("Power Puter")


class RgthreePowerPuter:
  """A powerful node that can compute and evaluate expressions and output as various types."""

  NAME = _NODE_NAME
  CATEGORY = get_category()

  @classmethod
  def INPUT_TYPES(cls):  # pylint: disable = invalid-name, missing-function-docstring
    return {
      "required": {},
      "optional": FlexibleOptionalInputType(any_type),
      "hidden": {
        "unique_id": "UNIQUE_ID",
        "extra_pnginfo": "EXTRA_PNGINFO",
        "prompt": "PROMPT",
      },
    }

  RETURN_TYPES = ByPassTypeTuple((any_type,))
  RETURN_NAMES = ByPassTypeTuple(("*",))
  FUNCTION = "main"

  @classmethod
  def IS_CHANGED(cls, **kwargs):
    """Forces a changed state if we could be unaware of data changes (like using `node()`)."""

    # Strip string literals and comments.
    code = kwargs['code']
    code = re.sub(r"'[^']+?'", "''", code)
    code = re.sub(r'"[^"]+?"', '""', code)
    code = re.sub(r'#.*\n', '\n', code)

    # If we have a non-deterministic function, then we'll always consider ourself changed since we
    # cannot be sure that the data would be the same (random, another unconnected node, etc).
    for check in _NON_DETERMINISTIC_FUNCTION_CHECKS:
      matches = re.search(check, code)
      if matches:
        log_node_warn(
          _NODE_NAME,
          f"Note, Power Puter (node #{kwargs['unique_id']}) cannot be cached b/c it's using a"
          f" non-deterministic function call. Matches function call for '{matches.group(1)}'."
        )
        return time.time()
    return 42

  def main(self, **kwargs):
    """Does the nodes' work."""
    code = kwargs['code']
    unique_id = kwargs['unique_id']
    pnginfo = kwargs['extra_pnginfo']
    workflow = pnginfo["workflow"] if "workflow" in pnginfo else {"nodes": []}
    prompt = kwargs['prompt']

    outputs = get_dict_value(kwargs, 'outputs.outputs', None)
    if not outputs:
      output = kwargs.get('output', None)
      if not output:
        output = 'STRING'
      outputs = [output]

    ctx = {}
    # Set variable names, defaulting to None instead of KeyErrors
    for c in list('abcdefghijklmnopqrstuvwxyz'):
      ctx[c] = kwargs[c] if c in kwargs else None

    # Clean the code before evaluating. For now, we just change usage of `input_node` so the passed
    # variable is a string, if it isn't (instead of `input_node(a)` it's to be `input_node('a')`.
    code = re.sub(r'input_node\(([^\'"].*?)\)', r'input_node("\1")', code)

    eva = _Puter(code=code, ctx=ctx, workflow=workflow, prompt=prompt, unique_id=unique_id)
    values = eva.execute()

    # Check if we have multiple outputs that the returned value is a tuple and raise if not.
    if len(outputs) > 1 and not isinstance(values, tuple):
      t = re.sub(r'^<[a-z]*\s(.*?)>$', r'\1', str(type(values)))
      msg = (
        f"When using multiple node outputs, the value from the code should be a 'tuple' with the"
        f" number of items equal to the number of outputs. But value from code was of type {t}."
      )
      log_node_error(_NODE_NAME, f'{msg}\n')
      raise ValueError(msg)

    if len(outputs) == 1:
      values = (values,)

    if len(values) > len(outputs):
      log_node_warn(
        _NODE_NAME,
        f"Expected value from code to be tuple with {len(outputs)} items, but value from code had"
        f" {len(values)} items. Extra values will be dropped."
      )
    elif len(values) < len(outputs):
      log_node_warn(
        _NODE_NAME,
        f"Expected value from code to be tuple with {len(outputs)} items, but value from code had"
        f" {len(values)} items. Extra outputs will be null."
      )

    # Now, we'll go over out return tuple, and cast as the output types.
    response = []
    for i, output in enumerate(outputs):
      value = values[i] if len(values) > i else None
      if value is not None:
        if output == 'INT':
          value = int(value)
        elif output == 'FLOAT':
          value = float(value)
        elif output == 'BOOL':
          value = bool(value)
        elif output == 'STRING':
          if isinstance(value, (dict, list)):
            value = json.dumps(value, indent=2)
          else:
            value = str(value)
        elif output == '*':
          # Do nothing, the output will be passed as-is. This could be anything and it's up to the
          # user to control the intended output, like passing through an input value, etc.
          pass
      response.append(value)
    return tuple(response)


class _Puter:
  """The main computation evaluator, using ast.parse the code.

  See https://www.basicexamples.com/example/python/ast for examples.
  """

  def __init__(self, *, code: str, ctx: dict[str, Any], workflow, prompt, unique_id):
    ctx = ctx or {}
    self._ctx = {**ctx}
    self._code = code
    self._workflow = workflow
    self._prompt = prompt
    self._prompt_nodes = []
    if self._prompt:
      self._prompt_nodes = [{'id': id} | {**node} for id, node in self._prompt.items()]
    self._prompt_node = [n for n in self._prompt_nodes if n['id'] == unique_id][0]

  def execute(self, code=Optional[str]) -> Any:
    """Evaluates a the code block."""
    code = code or self._code
    node = ast.parse(self._code)
    ctx = {**self._ctx}
    last_value = None
    for body in node.body:
      last_value = self._eval_statement(body, ctx)
      # If we got a return, then that's it folks.
      if isinstance(body, ast.Return):
        break
    return last_value

  def _get_nodes(self, node_id: Union[int, str, re.Pattern, None] = None) -> list[Any]:
    """Get a list of the nodes that match the node_id, or all the nodes in the prompt."""
    nodes = self._prompt_nodes.copy()
    if not node_id:
      return nodes

    if isinstance(node_id, re.Pattern):
      found = [n for n in nodes if re.search(node_id, get_dict_value(n, '_meta.title', ''))]
    else:
      node_id = str(node_id)
      found = None
      if re.match(r'\d+$', node_id):
        found = [n for n in nodes if node_id == n['id']]
      if not found:
        found = [n for n in nodes if node_id == get_dict_value(n, '_meta.title', '')]
    return found

  def _get_node(self, node_id: Union[int, str, re.Pattern, None] = None) -> Union[Any, None]:
    """Returns a prompt-node from the hidden prompt."""
    if node_id is None:
      return self._prompt_node
    nodes = self._get_nodes(node_id)
    if nodes and len(nodes) > 1:
      log_node_warn(_NODE_NAME, f"More than one node found for '{node_id}'. Returning first.")
    return nodes[0] if nodes else None

  def _get_input_node(self, input_name, node=None):
    """Gets the (non-muted) node of an input connection from a node (default to the power puter)."""
    node = node if node else self._prompt_node
    try:
      connected_node_id = node['inputs'][input_name][0]
      return [n for n in self._prompt_nodes if n['id'] == connected_node_id][0]
    except (TypeError, IndexError, KeyError):
      log_node_warn(_NODE_NAME, f'No input node found for "{input_name}". ')
    return None

  def _eval_statement(self, stmt: ast.stmt, ctx: dict, prev_stmt: Union[ast.stmt, None] = None):
    """Evaluates an ast.stmt."""

    if '__returned__' in ctx:
      return ctx['__returned__']

    # print('\n\n----: _eval_statement')
    # print(type(stmt))
    # print(ctx)

    if isinstance(stmt, (ast.FormattedValue, ast.Expr)):
      return self._eval_statement(stmt.value, ctx=ctx)

    if isinstance(stmt, (ast.Constant, ast.Num)):
      return stmt.n

    if isinstance(stmt, ast.BinOp):
      left = self._eval_statement(stmt.left, ctx=ctx)
      right = self._eval_statement(stmt.right, ctx=ctx)
      return _OPERATORS[type(stmt.op)](left, right)

    if isinstance(stmt, ast.BoolOp):
      left = self._eval_statement(stmt.values[0], ctx=ctx)
      # If we're an AND and already false, then don't even evaluate the right.
      if isinstance(stmt.op, ast.And) and not left:
        return left
      right = self._eval_statement(stmt.values[1], ctx=ctx)
      return _OPERATORS[type(stmt.op)](left, right)

    if isinstance(stmt, ast.UnaryOp):
      return _OPERATORS[type(stmt.op)](self._eval_statement(stmt.operand, ctx=ctx))

    if isinstance(stmt, (ast.Attribute, ast.Subscript)):
      # Like: node(14).inputs.sampler_name (Attribute)
      # Like: node(14)['inputs']['sampler_name'] (Subscript)
      item = self._eval_statement(stmt.value, ctx=ctx)
      attr = None
      if hasattr(stmt, 'attr'):
        attr = stmt.attr
      else:
        # Slice could be a name or a constant; evaluate it
        attr = self._eval_statement(stmt.slice, ctx=ctx)
      try:
        val = item[attr]
      except (TypeError, IndexError, KeyError):
        try:
          val = getattr(item, attr)
        except AttributeError:
          # If we're a dict, then just return None instead of error; saves time.
          if isinstance(item, dict):
            # Any special cases in the _SPECIAL_FUNCTIONS
            class_type = get_dict_value(item, "class_type")
            if class_type in _SPECIAL_FUNCTIONS and attr in _SPECIAL_FUNCTIONS[class_type]:
              val = _SPECIAL_FUNCTIONS[class_type][attr]
              # If our previous statment was a Call, then send back a tuple of the callable and
              # the evaluated item, and it will make the call; perhaps also adding other arguments
              # only it knows about.
              if isinstance(prev_stmt, ast.Call):
                return (val, item)
              val = val(item)
            else:
              val = None
          else:
            raise
      return val

    if isinstance(stmt, (ast.List, ast.Tuple)):
      value = []
      for elt in stmt.elts:
        value.append(self._eval_statement(elt, ctx=ctx))
      return tuple(value) if isinstance(stmt, ast.Tuple) else value

    # f-strings: https://www.basicexamples.com/example/python/ast-JoinedStr
    # Note, this will str() all evaluated items in the fstrings, and doesn't handle f-string
    # directives, like padding, etc.
    if isinstance(stmt, ast.JoinedStr):
      vals = [str(self._eval_statement(v, ctx=ctx)) for v in stmt.values]
      val = ''.join(vals)
      return val

    if isinstance(stmt, ast.Name):
      if stmt.id in ctx:
        val = ctx[stmt.id]
        return val
      raise NameError(f"Name not found: {stmt.id}")

    if isinstance(stmt, ast.ListComp):
      # Like: [v.lora for name, v in node(19).inputs.items() if name.startswith('lora_')]
      # Like: [v.lower() for v in lora_list]
      # Like: [v for v in l if v.startswith('B')]
      # Like: [v.lower() for v in l if v.startswith('B') or v.startswith('F')]
      # ---
      # Like: [l for n in nodes(re('Loras')).values() if (l := n.loras)]
      final_list = []

      gen_ctx = {**ctx}

      generators = [*stmt.generators]

      def handle_gen(generators: list[ast.comprehension]):
        gen = generators.pop(0)
        if isinstance(gen.target, ast.Name):
          gen_ctx[gen.target.id] = None
        elif isinstance(gen.target, ast.Tuple):  # dict, like `for k, v in d.entries()`
          for elt in gen.target.elts:
            gen_ctx[elt.id] = None
        else:
          raise ValueError('Na')

        # A call, like my_dct.items(), or a named ctx list
        if isinstance(gen.iter, ast.Call):
          gen_iters = self._eval_statement(gen.iter, ctx=gen_ctx)
        elif isinstance(gen.iter, (ast.Name, ast.Attribute, ast.List, ast.Tuple)):
          gen_iters = self._eval_statement(gen.iter, ctx=gen_ctx)

        for gen_iter in gen_iters:
          if_ctx = {**gen_ctx}
          if isinstance(gen.target, ast.Tuple):  # dict, like `for k, v in d.entries()`
            for i, elt in enumerate(gen.target.elts):
              if_ctx[elt.id] = gen_iter[i]
          else:
            if_ctx[gen.target.id] = gen_iter
          good = True
          for ifcall in gen.ifs:
            if not self._eval_statement(ifcall, ctx=if_ctx):
              good = False
              break
          if not good:
            continue
          gen_ctx.update(if_ctx)
          if len(generators):
            handle_gen(generators)
          else:
            final_list.append(self._eval_statement(stmt.elt, gen_ctx))
        generators.insert(0, gen)

      handle_gen(generators)
      return final_list

    if isinstance(stmt, ast.Call):
      call = None
      args = []
      kwargs = {}
      if isinstance(stmt.func, ast.Attribute):
        call = self._eval_statement(stmt.func, prev_stmt=stmt, ctx=ctx)
        if isinstance(call, tuple):
          args.append(call[1])
          call = call[0]
        if not call:
          raise ValueError(f'No call for ast.Call {stmt.func}')
      if isinstance(stmt.func, ast.Name):
        name = stmt.func.id
        if name in _FUNCTIONS:
          fn = _FUNCTIONS[name]
          call = fn.call
          if isinstance(call, str):
            call = getattr(self, call)
          num_args = len(stmt.args)
          if num_args < fn.args[0] or (fn.args[1] is not None and num_args > fn.args[1]):
            toErr = " or more" if fn.args[1] is None else f" to {fn.args[1]}"
            raise SyntaxError(f"Invalid function call: {fn.name} requires {fn.args[0]}{toErr} args")
        if not call:
          raise ValueError(f'No call for ast.Call {name}')
      for arg in stmt.args:
        args.append(self._eval_statement(arg, ctx=ctx))
      for kwarg in stmt.keywords:
        kwargs[kwarg.arg] = self._eval_statement(kwarg.value, ctx=ctx)
      return call(*args, **kwargs)

    if isinstance(stmt, ast.Compare):
      l = self._eval_statement(stmt.left, ctx=ctx)
      r = self._eval_statement(stmt.comparators[0], ctx=ctx)
      if isinstance(stmt.ops[0], ast.Eq):
        return 1 if l == r else 0
      if isinstance(stmt.ops[0], ast.NotEq):
        return 1 if l != r else 0
      if isinstance(stmt.ops[0], ast.Gt):
        return 1 if l > r else 0
      if isinstance(stmt.ops[0], ast.GtE):
        return 1 if l >= r else 0
      if isinstance(stmt.ops[0], ast.Lt):
        return 1 if l < r else 0
      if isinstance(stmt.ops[0], ast.LtE):
        return 1 if l <= r else 0
      if isinstance(stmt.ops[0], ast.In):
        return 1 if l in r else 0
      raise NotImplementedError("Operator " + stmt.ops[0].__class__.__name__ + " not supported.")

    if isinstance(stmt, (ast.If, ast.IfExp)):
      value = self._eval_statement(stmt.test, ctx=ctx)
      if value:
        # ast.If is a list, ast.IfExp is an object.
        bodies = stmt.body if isinstance(stmt.body, list) else [stmt.body]
        for body in bodies:
          value = self._eval_statement(body, ctx=ctx)
      elif stmt.orelse:
        # ast.If is a list, ast.IfExp is an object. TBH, I don't know why the If is a list, it's
        # only ever one item AFAICT.
        orelses = stmt.orelse if isinstance(stmt.orelse, list) else [stmt.orelse]
        for orelse in orelses:
          value = self._eval_statement(orelse, ctx=ctx)
      return value

    # Assign a variable and add it to our ctx.
    if isinstance(stmt, ast.Assign):
      value = self._eval_statement(stmt.value, ctx=ctx)
      if len(stmt.targets) != 1:
        raise ValueError('Expected length of assign targets to be 1')
      target = stmt.targets[0]
      if isinstance(target, ast.Tuple):
        for i, elt in enumerate(target.elts):
          ctx[elt.id] = value[i]
      else:  # Should be ast.Name
        ctx[target.id] = value
      return value

    # For assigning a var in a list comprehension.
    # Like [name for node in node_list if (name := node.name)]
    if isinstance(stmt, ast.NamedExpr):
      value = self._eval_statement(stmt.value, ctx=ctx)
      ctx[stmt.target.id] = value
      return value

    if isinstance(stmt, ast.Return):
      value = self._eval_statement(stmt.value, ctx=ctx)
      # Mark that we have a return value, as we may be deeper in evaluation, like going through an
      # if condition's body.
      ctx['__returned__'] = value
      return value

    raise TypeError(stmt)
