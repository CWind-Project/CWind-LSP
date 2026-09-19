"""Shared rendering of frontend type/FnDecl nodes for hover and completion."""

from __future__ import annotations

from typing import Optional

from cwind_frontend.ast_components.ast import ConstDecl, EnumDecl, FnDecl, StructDecl, TraitDecl, Type, TypeDecl


def bare_type(name: str) -> str:
    name = str(name)
    if name.startswith("std::builtins::"):
        name = name[len("std::builtins::") :]
    if "::" in name and not name.startswith(("*", "[", "fn")):
        name = name.split("::")[-1]
    return name


def render_type(node: Optional[Type]) -> str:
    if node is None:
        return "None"
    name = getattr(node, "_fqn_original", None) or node.name
    name = bare_type(name)
    if node.args:
        name += "<" + ", ".join(render_type(arg) for arg in node.args) + ">"
    if node.ref:
        name = "&" + ("mut " if node.mut else "") + name
    return name


def render_param(param) -> str:
    info = getattr(param, "_typed_ann", None)
    if param.name == "self":
        if param.type is not None:
            if param.type.ref:
                return "&mut self" if param.type.mut else "&self"
            return "self"
        if isinstance(info, dict):
            resolved = info.get("type")
            if isinstance(resolved, dict) and resolved.get("ref"):
                return "&mut self" if resolved.get("mut") else "&self"
        return "self"
    if isinstance(info, dict):
        from .analysis import format_type_info

        rendered = format_type_info(info.get("type"))
        if rendered:
            return f"{param.name}: {rendered}"
    return f"{param.name}: {render_type(param.type)}"


def render_signature(fn: FnDecl) -> str:
    rendered = [render_param(param) for param in fn.params]
    if getattr(fn, "variadic", False):
        rendered.append("...")
    params = ", ".join(rendered)
    generics = ""
    if fn.type_params:
        generics = "<" + ", ".join(param.name for param in fn.type_params) + ">"
    returns = render_type(fn.return_type) if fn.return_type is not None else "None"
    prefix = "fn"
    if isinstance(getattr(fn, "extern_abi", None), str):
        prefix = f'extern "{fn.extern_abi}" fn'
    owner = getattr(fn, "cwind_owner", None)
    if owner is not None:
        return f"{prefix} {render_type(owner)}::{fn.name}{generics}({params}) -> {returns}"
    return f"{prefix} {fn.name}{generics}({params}) -> {returns}"


def render_declaration(node) -> str:
    if isinstance(node, StructDecl):
        return f"struct {node.name}"
    if isinstance(node, EnumDecl):
        return f"enum {node.name}"
    if isinstance(node, TraitDecl):
        return f"trait {node.name}"
    if isinstance(node, TypeDecl):
        return f"type {node.name} = {render_type(node.base)}"
    if isinstance(node, ConstDecl):
        return f"const {node.name}: {render_type(node.type)}"
    return type(node).__name__
