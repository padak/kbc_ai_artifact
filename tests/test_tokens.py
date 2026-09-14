import pytest

from src.tokens import (
    EMITTED_TYPES,
    TokenLimits,
    TokenSet,
    TokenValidationError,
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
