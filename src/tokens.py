"""KBC DTCG profile: parse, validate, merge, resolve and emit design tokens.

Implements the subset of the W3C Design Tokens Community Group format
(2025.10) that the hub supports (spec: docs/superpowers/specs/
2026-09-14-design-systems-design.md, Key decision 7). Stdlib only. Nothing
here touches Storage, settings or HTTP: callers pass limits in and get pure
values out, so every function is safe to cache and trivial to test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator, Literal, NamedTuple

#: Types the hub emits as CSS custom properties.
EMITTED_TYPES = frozenset(
    {"color", "dimension", "fontFamily", "fontWeight", "number", "duration",
     "cubicBezier", "shadow", "border", "typography"}
)
#: Types the hub stores and reports but does not emit (warning at submit).
PRESERVED_TYPES = frozenset({"gradient", "strokeStyle", "transition"})
KNOWN_TYPES = EMITTED_TYPES | PRESERVED_TYPES
#: The DTCG reserved keys a token or group may carry.
DTCG_KEYS = frozenset({"$value", "$type", "$description", "$extensions", "$deprecated"})
#: ``{path.to.token}`` alias syntax. No JSON-Pointer references (422 upstream).
ALIAS_RE = re.compile(r"^\{([^{}]+)\}$")
#: ``typography`` composite fields and the CSS suffix each one emits under.
TYPOGRAPHY_SUBS: tuple[tuple[str, str], ...] = (
    ("fontFamily", "font-family"), ("fontSize", "font-size"),
    ("fontWeight", "font-weight"), ("lineHeight", "line-height"),
    ("letterSpacing", "letter-spacing"),
)


class TokenError(ValueError):
    """One validation finding, addressed by a JSON Pointer into the request."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


class TokenValidationError(ValueError):
    """Every fatal finding of one validation pass, in document order."""

    def __init__(self, findings: list[dict[str, str]]) -> None:
        super().__init__(f"{len(findings)} token finding(s)")
        self.findings = findings


class Token(NamedTuple):
    path: tuple[str, ...]
    type: str | None          # None only transiently for an alias leaf (resolve() fills it)
    value: Any                # raw $value; aliases kept as "{a.b}" strings
    description: str | None


@dataclass(frozen=True)
class TokenLimits:
    max_depth: int
    max_tokens: int
    max_alias_depth: int


def _pointer(base: str, *segments: str) -> str:
    escaped = [s.replace("~", "~0").replace("/", "~1") for s in segments]
    return base + "".join("/" + s for s in escaped)


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def variable_name(path: tuple[str, ...]) -> str:
    """The CSS custom-property name a token path emits, or "" when it normalises away."""
    joined = "-".join(path).lower()
    collapsed = _NON_ALNUM.sub("-", joined).strip("-")
    return "--" + collapsed if collapsed else ""


def emitted_names(token: Token) -> list[tuple[str, str]]:
    """(variables()-key, --name) pairs a token emits; five for typography."""
    base = variable_name(token.path)
    dotted = ".".join(token.path)
    if token.type == "typography":
        return [(f"{dotted}/{suffix}", f"{base}-{suffix}") for _, suffix in TYPOGRAPHY_SUBS]
    return [(dotted, base)]


def is_alias(value: Any) -> bool:
    return isinstance(value, str) and ALIAS_RE.match(value) is not None


def alias_target(value: str) -> tuple[str, ...]:
    return tuple(ALIAS_RE.match(value).group(1).split("."))


class TokenSet:
    """An ordered set of tokens keyed by path tuple."""

    def __init__(self, tokens: dict[tuple[str, ...], Token],
                 limits: TokenLimits | None = None) -> None:
        self.tokens = tokens
        #: The limits this set was validated under, so emission can bound its
        #: own alias descent without every caller having to thread them through.
        self.limits = limits

    @classmethod
    def parse(cls, document: dict, *, limits: TokenLimits, pointer: str = "/tokens") -> "TokenSet":
        findings: list[TokenError] = []
        tokens: dict[tuple[str, ...], Token] = {}
        if not isinstance(document, dict):
            raise TokenValidationError([TokenError(pointer, "must be an object").as_dict()])
        cls._walk(document, (), None, pointer, 0, limits, tokens, findings)
        if not tokens and not findings:
            findings.append(TokenError(pointer, "document holds no tokens"))
        seen: dict[str, tuple[str, ...]] = {}
        for tok in tokens.values():
            if variable_name(tok.path) == "":
                findings.append(TokenError(_pointer(pointer, *tok.path),
                                           "path normalises to an empty CSS variable name"))
                continue
            for _, name in emitted_names(tok):
                other = seen.get(name)
                if other is not None and other != tok.path:
                    findings.append(TokenError(_pointer(pointer, *tok.path),
                        f"CSS variable {name} collides with token {'.'.join(other)}"))
                seen.setdefault(name, tok.path)
        if len(tokens) > limits.max_tokens:
            findings.append(TokenError(pointer, f"more than {limits.max_tokens} tokens"))
        if findings:
            raise TokenValidationError([f.as_dict() for f in findings])
        return cls(tokens, limits)

    @classmethod
    def _walk(cls, node: dict, path: tuple[str, ...], inherited: str | None, ptr: str,
              depth: int, limits: TokenLimits, out: dict, findings: list[TokenError],
              *, require_type: bool = True) -> None:
        if depth > limits.max_depth:
            findings.append(TokenError(ptr, f"nested deeper than {limits.max_depth} levels"))
            return
        for key in node:
            if key.startswith("$") and key not in DTCG_KEYS:
                findings.append(TokenError(_pointer(ptr, key), f"unknown reserved key '{key}'"))
        node_type = node.get("$type", inherited)
        if "$type" in node and node["$type"] not in KNOWN_TYPES:
            findings.append(TokenError(_pointer(ptr, "$type"), f"unknown $type '{node['$type']}'"))
            node_type = None
        if "$value" in node:
            value = node["$value"]
            if require_type and node_type is None and not is_alias(value):
                findings.append(TokenError(ptr, "token has no resolved $type"))
            # A leaf is a leaf: children hanging off a token would be dropped
            # silently, so reject the document instead of losing them.
            if any(not key.startswith("$") for key in node):
                findings.append(TokenError(ptr, "a token may not also contain child groups"))
            desc = node.get("$description")
            if desc is not None and not isinstance(desc, str):
                findings.append(TokenError(_pointer(ptr, "$description"), "must be a string"))
                desc = None
            out[path] = Token(path, node_type, value, desc)
            return
        for key, child in node.items():
            if key.startswith("$"):
                continue
            if not isinstance(child, dict):
                findings.append(TokenError(_pointer(ptr, key), "must be a group or a token object"))
                continue
            cls._walk(child, path + (key,), node_type, _pointer(ptr, key), depth + 1, limits, out,
                      findings, require_type=require_type)

    # --- resolution -------------------------------------------------------
    def resolve(self, *, limits: TokenLimits, pointer: str = "/tokens") -> None:
        """Fill alias types, reject cycles, dangling ends, chains too long and
        type mismatches. Idempotent."""
        self.limits = limits
        findings: list[TokenError] = []
        for tok in list(self.tokens.values()):
            if not is_alias(tok.value):
                continue
            try:
                target = self._follow(tok, limits.max_alias_depth)
            except TokenError as err:
                findings.append(TokenError(_pointer(pointer, *tok.path), err.message))
                continue
            if tok.type is None:
                self.tokens[tok.path] = tok._replace(type=target.type)
            elif target.type is not None and target.type != tok.type:
                findings.append(TokenError(_pointer(pointer, *tok.path),
                    f"a {tok.type} token aliases a {target.type} token ({'.'.join(target.path)})"))
        for tok in self.tokens.values():
            if tok.type is None:
                findings.append(TokenError(_pointer(pointer, *tok.path), "token has no resolved $type"))
        if findings:
            raise TokenValidationError([f.as_dict() for f in findings])
        # typography aliases now emit five names: re-run the collision check
        seen: dict[str, tuple[str, ...]] = {}
        for tok in self.tokens.values():
            for _, name in emitted_names(tok):
                other = seen.setdefault(name, tok.path)
                if other != tok.path:
                    findings.append(TokenError(_pointer(pointer, *tok.path),
                        f"CSS variable {name} collides with token {'.'.join(other)}"))
        if findings:
            raise TokenValidationError([f.as_dict() for f in findings])

    def _follow(self, tok: Token, max_depth: int) -> Token:
        seen: list[tuple[str, ...]] = [tok.path]
        cur = tok
        while is_alias(cur.value):
            if len(seen) > max_depth:
                raise TokenError("", f"alias chain longer than {max_depth}")
            target_path = alias_target(cur.value)
            nxt = self.tokens.get(target_path)
            if nxt is None:
                raise TokenError("", f"unknown token '{'.'.join(target_path)}'")
            if target_path in seen:
                raise TokenError("", f"alias cycle through {'.'.join(target_path)}")
            seen.append(target_path)
            cur = nxt
        return cur

    def resolved(self, path: tuple[str, ...]) -> Token:
        """The concrete, non-alias token at the end of ``path``'s alias chain."""
        tok = self.tokens[path]
        return self._follow(tok, len(self.tokens) + 1) if is_alias(tok.value) else tok

    # --- modes ------------------------------------------------------------
    def merged(self, overrides: dict, *, limits: TokenLimits, pointer: str = "/modes/dark") -> "TokenSet":
        """This set with ``overrides`` (a DTCG document) applied by path."""
        findings: list[TokenError] = []
        parsed: dict[tuple[str, ...], Token] = {}
        # Types may be omitted in an override, so walk without the type check:
        # _walk records type None for those, and we fill it from the base.
        self._walk(overrides, (), None, pointer, 0, limits, parsed, findings, require_type=False)
        merged = dict(self.tokens)
        for path, over in parsed.items():
            base_tok = self.tokens.get(path)
            if base_tok is None:
                findings.append(TokenError(_pointer(pointer, *path), "override of a token absent from the base"))
                continue
            if over.type is not None and over.type != base_tok.type:
                findings.append(TokenError(_pointer(pointer, *path),
                    f"override may not change the type ({base_tok.type} -> {over.type})"))
                continue
            merged[path] = Token(path, base_tok.type, over.value,
                                 over.description if over.description is not None else base_tok.description)
        if findings:
            raise TokenValidationError([f.as_dict() for f in findings])
        out = TokenSet(merged, limits)
        out.resolve(limits=limits, pointer=pointer)
        return out

    def variables(self) -> dict[str, str]:
        """``"path.to.token" -> "--variable"``; typography maps its five sub-names."""
        out: dict[str, str] = {}
        for tok in self.tokens.values():
            for key, name in emitted_names(tok):
                out[key] = name
        return dict(sorted(out.items()))

    def by_dotted(self, dotted: str) -> Token | None:
        return self.tokens.get(tuple(dotted.split(".")))

    def __iter__(self) -> Iterator[Token]:
        return iter(self.tokens.values())

    def __len__(self) -> int:
        return len(self.tokens)


# --- CSS emission ---------------------------------------------------------

_BREAKOUT = re.compile(r"[}<;]|/\*|\*/|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _check_css_text(text: str, path: tuple[str, ...]) -> str:
    if _BREAKOUT.search(text):
        raise TokenError(_pointer("/tokens", *path),
                         "value contains characters that could break out of a <style> block")
    return text


def _quote_family(name: str) -> str:
    name = name.strip()
    return f'"{name}"' if " " in name and not name.startswith('"') else name


def _num(x: Any) -> str:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise TokenError("", f"expected a number, got {x!r}")
    if float(x).is_integer():
        return str(int(x))
    # Fixed notation only: repr() would emit 1e-07, which CSS parses as a
    # <number> in some positions and as nothing at all in others.
    return f"{float(x):.6f}".rstrip("0").rstrip(".") or "0"


def _dim(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, dict) and v.get("unit") in ("px", "rem"):
        return _num(v["value"]) + v["unit"]
    raise TokenError("", f"unsupported dimension {v!r}")


def _color(v: Any) -> str:
    if isinstance(v, str):
        return v
    if not isinstance(v, dict):
        raise TokenError("", f"unsupported color {v!r}")
    space = v.get("colorSpace", "srgb")
    if space != "srgb":
        raise TokenError("", f"colorSpace '{space}' is not supported in this release (sRGB only)")
    alpha = v.get("alpha", 1)
    if v.get("hex") and alpha == 1:
        return str(v["hex"])
    comps = v.get("components")
    if not (isinstance(comps, list) and len(comps) == 3):
        raise TokenError("", "sRGB color needs three components")
    r, g, b = (max(0, min(255, round(float(c) * 255))) for c in comps)
    return f"rgb({r} {g} {b} / {_num(alpha)})" if alpha != 1 else f"rgb({r} {g} {b})"


def _duration(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, dict) and v.get("unit") in ("ms", "s"):
        return _num(v["value"]) + v["unit"]
    raise TokenError("", f"unsupported duration {v!r}")


#: The token type each composite sub-field must have when it is an alias.
_SHADOW_FIELD_TYPES = {"offsetX": "dimension", "offsetY": "dimension", "blur": "dimension",
                       "spread": "dimension", "color": "color"}
_BORDER_FIELD_TYPES = {"width": "dimension", "color": "color"}
_TYPO_FIELD_TYPES = {"fontFamily": "fontFamily", "fontSize": "dimension", "fontWeight": "fontWeight",
                     "lineHeight": "number", "letterSpacing": "dimension"}

_Seen = tuple[tuple[str, ...], ...]


def _alias_budget(tokenset: TokenSet, limits: TokenLimits | None) -> int:
    """How many composite sub-field hops one emission may descend."""
    if limits is not None:
        return limits.max_alias_depth
    if tokenset.limits is not None:
        return tokenset.limits.max_alias_depth
    return len(tokenset) + 1      # structural bound; the visited set is what terminates


def _subfield_alias(tokenset: TokenSet, expected_type: str | None, value: str,
                    limits: TokenLimits | None, seen: _Seen) -> str:
    """Resolve one alias sitting inside a composite value.

    ``resolve()`` only follows whole-``$value`` aliases, so this descent is the
    only thing standing between a self-referential composite and a
    RecursionError: it is bounded by ``max_alias_depth``, remembers the tokens
    already entered, and checks the target's type against the field's.
    """
    target = alias_target(value)
    if target in seen:
        raise TokenError("", f"alias cycle through {'.'.join(target)}")
    budget = _alias_budget(tokenset, limits)
    if len(seen) >= budget:
        raise TokenError("", f"alias chain longer than {budget}")
    if target not in tokenset.tokens:
        raise TokenError("", f"unknown token '{'.'.join(target)}'")
    end = tokenset.resolved(target)
    if expected_type is not None and end.type is not None and end.type != expected_type:
        raise TokenError("", f"a {expected_type} token aliases a {end.type} token "
                             f"({'.'.join(end.path)})")
    return _concrete(tokenset, target, limits, seen + (target,))


def _shadow(tokenset: TokenSet, v: Any, path: tuple[str, ...],
            limits: TokenLimits | None, seen: _Seen) -> str:
    items = v if isinstance(v, list) else [v]
    parts = []
    for s in items:
        if not isinstance(s, dict):
            raise TokenError("", "shadow must be an object or a list of objects")

        def field(name: str, default: Any = None) -> str:
            raw = s.get(name, default) if default is not None else s[name]
            return _scalar(tokenset, _SHADOW_FIELD_TYPES[name], raw, path, limits, seen)

        inset = "inset " if s.get("inset") else ""
        parts.append(f"{inset}{field('offsetX')} {field('offsetY')} {field('blur')} "
                     f"{field('spread', '0px')} {field('color')}")
    return ", ".join(parts)


def _border(tokenset: TokenSet, v: Any, path: tuple[str, ...],
            limits: TokenLimits | None, seen: _Seen) -> str:
    width = _scalar(tokenset, _BORDER_FIELD_TYPES["width"], v["width"], path, limits, seen)
    color = _scalar(tokenset, _BORDER_FIELD_TYPES["color"], v["color"], path, limits, seen)
    return f"{width} {v['style']} {color}"


def _scalar(tokenset: TokenSet, tok_type: str | None, value: Any, path: tuple[str, ...],
            limits: TokenLimits | None = None, seen: _Seen = ()) -> str:
    """Concrete CSS text for a non-typography value; nested aliases are followed."""
    if is_alias(value):                       # nested alias inside a composite
        return _subfield_alias(tokenset, tok_type, value, limits, seen)
    if tok_type == "color":
        out = _color(value)
    elif tok_type == "dimension":
        out = _dim(value)
    elif tok_type == "fontFamily":
        out = ", ".join(_quote_family(f) for f in value) if isinstance(value, list) else _quote_family(str(value))
    elif tok_type in ("fontWeight", "number"):
        out = str(value) if isinstance(value, str) else _num(value)
    elif tok_type == "duration":
        out = _duration(value)
    elif tok_type == "cubicBezier":
        out = "cubic-bezier(" + ", ".join(_num(x) for x in value) + ")"
    elif tok_type == "shadow":
        out = _shadow(tokenset, value, path, limits, seen)
    elif tok_type == "border":
        out = _border(tokenset, value, path, limits, seen)
    else:
        raise TokenError("", f"type '{tok_type}' is not emitted")
    return _check_css_text(out, path)


def _typography_parts(tokenset: TokenSet, value: dict, path: tuple[str, ...],
                      limits: TokenLimits | None = None,
                      seen: _Seen = ()) -> list[tuple[str, str]]:
    out = []
    for field, suffix in TYPOGRAPHY_SUBS:
        if field not in value:
            raise TokenError("", f"typography value lacks '{field}'")
        out.append((suffix, _scalar(tokenset, _TYPO_FIELD_TYPES[field], value[field],
                                    path, limits, seen)))
    return out


def css_value(token: Token, tokenset: TokenSet) -> list[tuple[str, str]]:
    """``(--name, css text)`` pairs one token emits; empty for preserved types."""
    if token.type in PRESERVED_TYPES:
        return []
    name = variable_name(token.path)
    try:
        if is_alias(token.value):
            target = tokenset.resolved(token.path)
            if target.type == "typography":
                tname = variable_name(target.path)
                return [(f"{name}-{s}", f"var({tname}-{s})") for _, s in TYPOGRAPHY_SUBS]
            return [(name, f"var({variable_name(alias_target(token.value))})")]
        if token.type == "typography":
            if not isinstance(token.value, dict):
                raise TokenError("", "typography value must be an object")
            return [(f"{name}-{s}", v) for s, v in _typography_parts(tokenset, token.value, token.path)]
        return [(name, _scalar(tokenset, token.type, token.value, token.path))]
    except TokenError as err:
        raise TokenValidationError([{"path": _pointer("/tokens", *token.path),
                                     "message": err.message}]) from err
    except (KeyError, TypeError, ValueError) as err:
        raise TokenValidationError([{"path": _pointer("/tokens", *token.path),
                                     "message": f"malformed {token.type} value: {err}"}]) from err


def concrete_value(tokenset: TokenSet, path: tuple[str, ...], *,
                   limits: TokenLimits | None = None) -> str:
    """Alias-followed concrete CSS value; typography collapses to a ``font`` shorthand."""
    return _concrete(tokenset, path, limits, ())


def _concrete(tokenset: TokenSet, path: tuple[str, ...], limits: TokenLimits | None,
              seen: _Seen) -> str:
    tok = tokenset.resolved(path)
    if tok.type == "typography":
        parts = dict(_typography_parts(tokenset, tok.value, tok.path, limits, seen))
        return f"{parts['font-weight']} {parts['font-size']}/{parts['line-height']} {parts['font-family']}"
    return _scalar(tokenset, tok.type, tok.value, tok.path, limits, seen)


def _block(selector: str, pairs: list[tuple[str, str]], indent: str = "") -> str:
    lines = [f"{indent}{selector} {{"]
    lines.extend(f"{indent}  {name}: {value};" for name, value in pairs)
    lines.append(f"{indent}}}")
    return "\n".join(lines) + "\n"


def _pairs(ts: TokenSet) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for tok in ts:
        out.extend(css_value(tok, ts))
    return out


def to_css(base: TokenSet, dark: TokenSet | None, *, mode: Literal["all", "light", "dark"]) -> str:
    """The stylesheet for one bundle: ``all`` adds the two dark blocks, the
    flat modes emit every token under ``:root``.

    ``mode="dark"`` on a bundle without a dark mode raises rather than quietly
    handing back the light set — the route turns that into a 404.
    """
    if mode == "dark" and dark is None:
        raise ValueError("no dark mode")
    if mode == "light" or dark is None:
        return _block(":root", _pairs(base))
    if mode == "dark":
        return _block(":root", _pairs(dark))
    light_pairs = _pairs(base)
    unchanged = set(light_pairs)          # hoisted: the limit allows thousands of tokens
    changed = [p for p in _pairs(dark) if p not in unchanged]
    return (_block(":root", light_pairs)
            + "@media (prefers-color-scheme: dark) {\n"
            + _block(':root:not([data-theme="light"])', changed, indent="  ")
            + "}\n"
            + _block(':root[data-theme="dark"]', changed))


def validate_document(document: dict, overrides: dict | None, *, limits: TokenLimits
                      ) -> tuple[TokenSet, TokenSet | None, list[dict[str, str]]]:
    """Parse + resolve base and dark, emit once to surface value errors, collect warnings."""
    base = TokenSet.parse(document, limits=limits)
    base.resolve(limits=limits)
    dark = base.merged(overrides, limits=limits) if overrides else None
    warnings: list[dict[str, str]] = []
    for tok in base:
        if tok.type in PRESERVED_TYPES:
            warnings.append({"path": _pointer("/tokens", *tok.path),
                             "message": f"type '{tok.type}' is preserved but not emitted as CSS"})
    _pairs(base)
    if dark is not None:
        _pairs(dark)
    return base, dark, warnings
