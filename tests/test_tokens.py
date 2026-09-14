import pytest

from src.tokens import (
    EMITTED_TYPES,
    TokenLimits,
    TokenSet,
    TokenValidationError,
    variable_name,
)

LIMITS = TokenLimits(max_depth=16, max_tokens=5000, max_alias_depth=32)


def test_parse_leaf_with_inherited_group_type():
    doc = {"color": {"$type": "color", "brand": {"primary": {"$value": "#3366ff",
           "$description": "Primary"}}}}
    ts = TokenSet.parse(doc, limits=LIMITS)
    tok = ts.by_dotted("color.brand.primary")
    assert tok.type == "color"
    assert tok.value == "#3366ff"
    assert tok.description == "Primary"
    assert tok.path == ("color", "brand", "primary")


def test_missing_type_is_a_finding_with_pointer():
    doc = {"spacing": {"md": {"$value": "16px"}}}
    with pytest.raises(TokenValidationError) as exc:
        TokenSet.parse(doc, limits=LIMITS)
    assert exc.value.findings == [
        {"path": "/tokens/spacing/md", "message": "token has no resolved $type"}
    ]


def test_unknown_type_is_rejected():
    doc = {"x": {"$type": "sparkle", "$value": "1"}}
    with pytest.raises(TokenValidationError) as exc:
        TokenSet.parse(doc, limits=LIMITS)
    assert "unknown $type 'sparkle'" in exc.value.findings[0]["message"]


def test_depth_and_count_limits():
    deep = {"$type": "number", "$value": 1}
    for _ in range(17):
        deep = {"g": deep}
    with pytest.raises(TokenValidationError) as exc:
        TokenSet.parse(deep, limits=LIMITS)
    assert "deeper than 16" in exc.value.findings[0]["message"]
    many = {f"t{i}": {"$type": "number", "$value": i} for i in range(6)}
    with pytest.raises(TokenValidationError) as exc:
        TokenSet.parse(many, limits=TokenLimits(16, 5, 32))
    assert "more than 5 tokens" in exc.value.findings[0]["message"]


def test_unknown_dollar_key_and_empty_document_rejected():
    with pytest.raises(TokenValidationError):
        TokenSet.parse({"a": {"$type": "number", "$value": 1, "$magic": 2}}, limits=LIMITS)
    with pytest.raises(TokenValidationError) as exc:
        TokenSet.parse({}, limits=LIMITS)
    assert exc.value.findings[0]["message"] == "document holds no tokens"


def test_all_findings_are_collected_not_just_the_first():
    doc = {"a": {"$value": 1}, "b": {"$value": 2}}
    with pytest.raises(TokenValidationError) as exc:
        TokenSet.parse(doc, limits=LIMITS)
    assert [f["path"] for f in exc.value.findings] == ["/tokens/a", "/tokens/b"]


def test_emitted_types_match_profile():
    assert EMITTED_TYPES == frozenset({"color", "dimension", "fontFamily", "fontWeight",
        "number", "duration", "cubicBezier", "shadow", "border", "typography"})


def test_variable_name_normalisation():
    assert variable_name(("color", "Brand Primary")) == "--color-brand-primary"
    assert variable_name(("Font/Size", "XL")) == "--font-size-xl"
    assert variable_name(("a", "--b--")) == "--a-b"


def test_empty_variable_name_is_a_finding():
    with pytest.raises(TokenValidationError) as exc:
        TokenSet.parse({"***": {"$type": "number", "$value": 1}}, limits=LIMITS)
    assert exc.value.findings[0]["message"] == "path normalises to an empty CSS variable name"


def test_collision_after_normalisation_is_a_finding():
    doc = {"a": {"b": {"$type": "number", "$value": 1}}, "A": {"B": {"$type": "number", "$value": 2}}}
    with pytest.raises(TokenValidationError) as exc:
        TokenSet.parse(doc, limits=LIMITS)
    assert "collides with" in exc.value.findings[0]["message"]


def test_typography_subnames_take_part_in_collisions():
    doc = {
        "heading": {"$type": "typography", "$value": {"fontFamily": "Inter", "fontSize": "2rem",
                    "fontWeight": 700, "lineHeight": 1.2, "letterSpacing": "0"}},
        "heading-font-size": {"$type": "dimension", "$value": "1px"},
    }
    with pytest.raises(TokenValidationError) as exc:
        TokenSet.parse(doc, limits=LIMITS)
    assert "--heading-font-size" in exc.value.findings[0]["message"]


def test_variables_map_lists_typography_subnames():
    doc = {"heading": {"$type": "typography", "$value": {"fontFamily": "Inter", "fontSize": "2rem",
           "fontWeight": 700, "lineHeight": 1.2, "letterSpacing": "0"}},
           "accent": {"$type": "color", "$value": "#fff"}}
    ts = TokenSet.parse(doc, limits=LIMITS)
    assert ts.variables() == {
        "accent": "--accent",
        "heading/font-family": "--heading-font-family",
        "heading/font-size": "--heading-font-size",
        "heading/font-weight": "--heading-font-weight",
        "heading/line-height": "--heading-line-height",
        "heading/letter-spacing": "--heading-letter-spacing",
    }


def _parsed(doc):
    ts = TokenSet.parse(doc, limits=LIMITS)
    ts.resolve(limits=LIMITS)
    return ts


def test_alias_inherits_type_and_resolves():
    ts = _parsed({"color": {"$type": "color", "a": {"$value": "#000"}, "b": {"$value": "{color.a}"}}})
    assert ts.by_dotted("color.b").type == "color"
    assert ts.resolved(("color", "b")).value == "#000"


def test_alias_cycle_and_dangling_are_findings():
    with pytest.raises(TokenValidationError) as exc:
        _parsed({"a": {"$type": "color", "$value": "{b}"}, "b": {"$type": "color", "$value": "{a}"}})
    assert "cycle" in exc.value.findings[0]["message"]
    with pytest.raises(TokenValidationError) as exc:
        _parsed({"a": {"$type": "color", "$value": "{nope}"}})
    assert "unknown token 'nope'" in exc.value.findings[0]["message"]


def test_alias_type_mismatch_is_a_finding():
    with pytest.raises(TokenValidationError) as exc:
        _parsed({"a": {"$type": "color", "$value": "#000"}, "b": {"$type": "dimension", "$value": "{a}"}})
    assert "aliases a color token" in exc.value.findings[0]["message"]


def test_alias_chain_depth_limit():
    doc = {"t0": {"$type": "number", "$value": 1}}
    for i in range(1, 40):
        doc[f"t{i}"] = {"$value": f"{{t{i - 1}}}"}
    with pytest.raises(TokenValidationError) as exc:
        _parsed(doc)
    assert "alias chain longer than 32" in exc.value.findings[0]["message"]


def test_merge_dark_overrides():
    base = _parsed({"color": {"$type": "color", "bg": {"$value": "#fff"}, "fg": {"$value": "#000"}}})
    dark = base.merged({"color": {"bg": {"$value": "#000"}}}, limits=LIMITS)
    assert dark.by_dotted("color.bg").value == "#000"
    assert dark.by_dotted("color.fg").value == "#000"      # untouched tokens carried over
    assert dark.by_dotted("color.bg").type == "color"      # type inherited from base


def test_merge_rejects_unknown_path_and_type_change():
    base = _parsed({"color": {"$type": "color", "bg": {"$value": "#fff"}}})
    with pytest.raises(TokenValidationError) as exc:
        base.merged({"color": {"other": {"$value": "#000"}}}, limits=LIMITS)
    assert exc.value.findings == [{"path": "/modes/dark/color/other",
                                   "message": "override of a token absent from the base"}]
    with pytest.raises(TokenValidationError) as exc:
        base.merged({"color": {"bg": {"$type": "dimension", "$value": "1px"}}}, limits=LIMITS)
    assert "may not change the type" in exc.value.findings[0]["message"]


def test_merge_detects_cycle_introduced_by_override():
    base = _parsed({"a": {"$type": "color", "$value": "#000"}, "b": {"$type": "color", "$value": "{a}"}})
    with pytest.raises(TokenValidationError):
        base.merged({"a": {"$value": "{b}"}}, limits=LIMITS)
