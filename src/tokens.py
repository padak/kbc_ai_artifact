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
from typing import Any, Iterator, NamedTuple

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

    def __init__(self, tokens: dict[tuple[str, ...], Token]) -> None:
        self.tokens = tokens

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
        return cls(tokens)

    @classmethod
    def _walk(cls, node: dict, path: tuple[str, ...], inherited: str | None, ptr: str,
              depth: int, limits: TokenLimits, out: dict, findings: list[TokenError]) -> None:
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
            if node_type is None and not is_alias(value):
                findings.append(TokenError(ptr, "token has no resolved $type"))
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
            cls._walk(child, path + (key,), node_type, _pointer(ptr, key), depth + 1, limits, out, findings)

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
