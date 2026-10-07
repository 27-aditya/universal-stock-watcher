"""Stock-extraction tiers: JSON-LD, cached selectors, LLM parsing."""

from __future__ import annotations

import json

import pytest
from conftest import html_page, product_json_ld

import watcher

# --------------------------------------------------------------------------
# tier 1: JSON-LD
# --------------------------------------------------------------------------

@pytest.mark.parametrize("availability,expected", [
    ("InStock", True),
    ("OutOfStock", False),
    ("SoldOut", False),
    ("PreOrder", False),
    ("LimitedAvailability", True),
    ("OnlineOnly", True),
    ("Discontinued", False),
    ("PreSale", False),
    ("BackOrder", False),
])
def test_json_ld_availability_values(availability, expected):
    assert watcher.check_json_ld(html_page(json_ld=product_json_ld(availability))) is expected


def test_json_ld_plain_value_not_url():
    ld = json.dumps({"@type": "Product", "offers": {"availability": "InStock"}})
    assert watcher.check_json_ld(html_page(json_ld=ld)) is True


def test_json_ld_availability_as_dict():
    ld = json.dumps({
        "@type": "Product",
        "offers": {"availability": {"@type": "InStock"}},
    })
    assert watcher.check_json_ld(html_page(json_ld=ld)) is True


def test_json_ld_offers_as_list_picks_first_known():
    ld = json.dumps({
        "@type": "Product",
        "offers": [
            {"price": "1", "availability": "https://schema.org/OutOfStock"},
            {"price": "2", "availability": "https://schema.org/InStock"},
        ],
    })
    assert watcher.check_json_ld(html_page(json_ld=ld)) is False


def test_json_ld_graph_wrapper():
    ld = json.dumps({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebPage", "name": "x"},
            {
                "@type": "Product",
                "offers": {"availability": "https://schema.org/InStock"},
            },
        ],
    })
    assert watcher.check_json_ld(html_page(json_ld=ld)) is True


def test_json_ld_top_level_offer_node():
    ld = json.dumps({"@type": "Offer", "availability": "https://schema.org/OutOfStock"})
    assert watcher.check_json_ld(html_page(json_ld=ld)) is False


def test_json_ld_nested_product_inside_array():
    ld = json.dumps([{"@type": "ItemList", "itemListElement": [{"item": {
        "@type": "Product",
        "offers": [{"availability": "https://schema.org/InStock"}],
    }}]}])
    assert watcher.check_json_ld(html_page(json_ld=ld)) is True


def test_json_ld_aggregate_offer_inventory_level():
    positive = json.dumps({"@type": "AggregateOffer", "inventoryLevel": {"value": 3}})
    zero = json.dumps({"@type": "AggregateOffer", "inventoryLevel": {"value": 0}})
    assert watcher.check_json_ld(html_page(json_ld=positive)) is True
    assert watcher.check_json_ld(html_page(json_ld=zero)) is False


def test_json_ld_unknown_availability_returns_none():
    ld = json.dumps({"@type": "Product", "offers": {"availability": "InStoreOnly"}})
    assert watcher.check_json_ld(html_page(json_ld=ld)) is None


def test_json_ld_absent_returns_none():
    assert watcher.check_json_ld("<html><body>nothing here</body></html>") is None


def test_json_ld_malformed_is_skipped_not_fatal():
    html = (
        '<script type="application/ld+json">{not valid json</script>'
        '<script type="application/ld+json">'
        + product_json_ld("InStock")
        + "</script>"
    )
    assert watcher.check_json_ld(html) is True


def test_json_ld_empty_script_tag():
    assert watcher.check_json_ld('<script type="application/ld+json"></script>') is None


def test_json_ld_ignores_non_product_schema():
    ld = json.dumps({"@context": "https://schema.org", "@type": "Organization",
                     "name": "Acme"})
    assert watcher.check_json_ld(html_page(json_ld=ld)) is None


# --------------------------------------------------------------------------
# tier 2: cached selector
# --------------------------------------------------------------------------

def test_cached_selector_in_stock():
    html = '<html><body><button class="buy">Add to cart</button></body></html>'
    assert watcher.check_cached_selector(html, ".buy") is True


def test_cached_selector_out_of_stock():
    html = '<html><body><div class="status">Out of stock</div></body></html>'
    assert watcher.check_cached_selector(html, ".status") is False


def test_cached_selector_ambiguous_returns_none():
    html = '<html><body><div class="status">Green, medium</div></body></html>'
    assert watcher.check_cached_selector(html, ".status") is None


def test_cached_selector_missing_element_returns_none():
    html = '<html><body><div class="other">In stock</div></body></html>'
    assert watcher.check_cached_selector(html, ".status") is None


def test_cached_selector_invalid_selector_is_swallowed():
    # bs4/soupsieve raises on malformed selectors - must not crash a run.
    assert watcher.check_cached_selector("<html></html>", "div[") is None


def test_cached_selector_empty_element_text_returns_none():
    html = '<html><body><div class="status">   </div></body></html>'
    assert watcher.check_cached_selector(html, ".status") is None


def test_cached_selector_none_or_empty_selector():
    html = '<html><body><div>In stock</div></body></html>'
    assert watcher.check_cached_selector(html, None) is None
    assert watcher.check_cached_selector(html, "") is None


# --------------------------------------------------------------------------
# LLM text prep + prompt
# --------------------------------------------------------------------------

def test_clean_text_strips_scripts_and_noise():
    html = (
        "<html><body><script>var x=1;</script><style>.a{}</style>"
        "<nav>menu</nav><footer>foot</footer><noscript>nojs</noscript>"
        "<p>Only this survives</p></body></html>"
    )
    text = watcher.clean_text_for_llm(html)
    assert "Only this survives" in text
    for noise in ("var x=1", ".a{}", "menu", "foot", "nojs"):
        assert noise not in text


def test_clean_text_truncates():
    html = "<html><body>" + ("word " * 5000) + "</body></html>"
    assert len(watcher.clean_text_for_llm(html, max_chars=100)) <= 100


def test_build_prompt_contains_product_and_page():
    prompt = watcher.build_prompt("Amul Whey 1kg", "chocolate flavour", "Add to cart now")
    assert "Amul Whey 1kg" in prompt
    assert "chocolate flavour" in prompt
    assert "Add to cart now" in prompt
    assert '"in_stock"' in prompt
    assert "css_hint" in prompt


# --------------------------------------------------------------------------
# LLM response parsing
# --------------------------------------------------------------------------

def test_parse_clean_json():
    v = watcher.parse_llm_response(
        '{"in_stock": true, "confidence": 0.9, "evidence": "add to cart", "css_hint": ".buy"}'
    )
    assert v is not None
    assert v.in_stock is True
    assert v.confidence == 0.9
    assert v.evidence == "add to cart"
    assert v.css_hint == ".buy"


def test_parse_fenced_json():
    raw = '```json\n{"in_stock": false, "confidence": 0.7, "evidence": "sold out"}\n```'
    v = watcher.parse_llm_response(raw)
    assert v is not None and v.in_stock is False


def test_parse_json_with_surrounding_prose():
    raw = 'Sure! Here is the answer:\n{"in_stock": true, "confidence": 0.8, "evidence": "buy now"}\nHope that helps.'
    v = watcher.parse_llm_response(raw)
    assert v is not None and v.in_stock is True


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("TRUE", True), (" true ", True),
    ("false", False), ("FALSE", False), (" false ", False),
    (1, True), (0, False), (True, True), (False, False),
])
def test_parse_accepts_truthy_boolean_representations(value, expected):
    v = watcher.parse_llm_response(json.dumps({"in_stock": value, "confidence": 0.5}))
    assert v is not None
    assert v.in_stock is expected


def test_parse_rejects_non_boolean_in_stock():
    assert watcher.parse_llm_response('{"in_stock": "maybe", "confidence": 0.9}') is None
    assert watcher.parse_llm_response('{"in_stock": 2, "confidence": 0.9}') is None


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "no json at all",
    "[]",
    '"just a string"',
    '{"confidence": 0.9}',           # missing in_stock
    '{"in_stock": true, "confidence":',  # truncated
    '{"in_stock": tru}',             # typo
])
def test_parse_rejects_unusable_responses(raw):
    assert watcher.parse_llm_response(raw) is None


def test_parse_clamps_confidence():
    assert watcher.parse_llm_response(
        '{"in_stock": true, "confidence": 99}'
    ).confidence == 1.0
    assert watcher.parse_llm_response(
        '{"in_stock": true, "confidence": -3}'
    ).confidence == 0.0


def test_parse_missing_confidence_defaults_to_zero():
    v = watcher.parse_llm_response('{"in_stock": true}')
    assert v is not None
    assert v.confidence == 0.0
    assert v.evidence == ""
    assert v.css_hint is None


def test_parse_non_numeric_confidence_defaults_to_zero():
    v = watcher.parse_llm_response('{"in_stock": true, "confidence": "high"}')
    assert v is not None and v.confidence == 0.0


@pytest.mark.parametrize("hint", [None, "", "   ", 42, {"sel": ".x"}])
def test_parse_css_hint_sanitised(hint):
    v = watcher.parse_llm_response(json.dumps({
        "in_stock": True, "confidence": 0.9, "css_hint": hint,
    }))
    assert v is not None
    assert v.css_hint is None or isinstance(v.css_hint, str)
    if isinstance(hint, str):
        assert v.css_hint == (hint.strip() or None)
