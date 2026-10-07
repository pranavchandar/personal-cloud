"""Self-check for the pure logic: EXIF coordinates, tag normalisation, model-output
parsing. No GPU, no network, no database. Run: python test_photoindex.py"""

from photoindex import _dms, clean_tags, parse_caption


def test_dms():
    assert abs(_dms((12, 58, 30.0), "N") - 12.975) < 1e-6
    assert abs(_dms((12, 58, 30.0), "S") + 12.975) < 1e-6   # south is negative
    assert abs(_dms((77, 35, 0.0), "W") + 77.583333) < 1e-5  # west is negative
    assert _dms(None, "N") is None
    assert _dms(("x", "y", "z"), "N") is None


def test_clean_tags():
    assert clean_tags(["Sunset ", "sunset", "SUNSET"]) == ["sunset"]  # one facet, not three
    assert clean_tags(["golden_hour"]) == ["golden hour"]
    # same facet, three surface forms the model actually emitted -> one row
    assert clean_tags(["black-and-white", "black and white"]) == ["black and white"]
    assert clean_tags(["3d-rendering"]) == ["3d rendering"]
    assert clean_tags(["water.", "  forest  "]) == ["water", "forest"]
    assert clean_tags(["a", "", None, "x" * 50]) == []   # too short / empty / too long
    assert clean_tags("not a list") == []
    assert clean_tags(None) == []
    assert len(clean_tags([f"tag{i}" for i in range(80)])) == 18  # capped at MAX_TAGS
    # prompt facet names echoed back as tags are noise, not description
    assert clean_tags(["mood", "Dominant Colors", "sunset"]) == ["sunset"]


def test_depadding_real_model_output():
    """Verbatim output from the calibration run: the model filled 21 slots with 14
    'fashion <x>' variants. Search needs distinct facets, not paraphrases."""
    observed = [
        "fashion comparison", "fashion contrast", "fashion differences",
        "fashion expectations", "fashion humor", "fashion meme", "fashion mismatch",
        "fashion preferences", "fashion reality", "fashion stereotypes",
        "fashion trend", "fashion trend analysis", "fashion trend prediction",
        "fashion trends", "shorts", "skirt", "thigh-high boots", "woman",
        "woman's body", "woman's fashion", "woman's legs",
    ]
    got = clean_tags(observed)
    fashion = [t for t in got if t.startswith("fashion")]
    assert len(fashion) <= 2, f"still padded: {fashion}"
    # the genuinely distinct facets must survive de-padding
    for keep in ("shorts", "skirt", "thigh high boots", "woman"):  # hyphen normalised
        assert keep in got, f"lost a real tag: {keep}"
    assert len(got) <= 18

    # and the 'mythical' case from the same run
    myth = clean_tags([
        "fantasy art", "human figure", "mythical", "mythical beast",
        "mythical creature", "mythical fantasy art", "mythical fantasy artwork",
        "mythical fantasy character", "mythical fantasy creature",
        "mythical fantasy illustration", "mythical fantasy landscape",
        "mythical fantasy scene", "mythical lion", "mythical winged creature",
        "mythical winged lion", "rocky outcrop", "sitting", "winged lion",
    ])
    assert len([t for t in myth if t.startswith("mythical")]) <= 2
    for keep in ("fantasy art", "winged lion", "rocky outcrop", "sitting"):
        assert keep in myth, f"lost a real tag: {keep}"

    # a legitimate shared head word must not be over-pruned
    ok = clean_tags(["golden hour", "golden light", "sunset", "warm"])
    assert ok == ["golden hour", "golden light", "sunset", "warm"]


def test_parse_caption():
    desc, tags = parse_caption('{"description": "A waterfall.", "tags": ["water", "forest"]}')
    assert desc == "A waterfall." and tags == ["water", "forest"]

    # models wrap JSON in prose or fences often enough that this must not be fatal
    desc, tags = parse_caption('Sure!\n```json\n{"description": "D", "tags": ["a b"]}\n```')
    assert desc == "D" and tags == ["a b"]

    # tags present, description missing -> still usable
    desc, tags = parse_caption('{"tags": ["plants"]}')
    assert desc == "" and tags == ["plants"]

    # truncated at max_new_tokens: shape taken verbatim from a real failure.
    # 15 good tags must not be discarded because the 16th was cut mid-word.
    desc, tags = parse_caption(
        '{\n  "tags": [\n    "architecture",\n    "urban",\n    "residential",\n'
        '    "house",\n    "roof',
    )
    assert tags == ["architecture", "urban", "residential", "house"], tags
    assert desc == ""

    # truncation that also carries a description
    desc, tags = parse_caption(
        '{"description": "A tall building.", "tags": ["urban", "tall buil'
    )
    assert desc == "A tall building." and tags == ["urban"]

    for junk in ("", "no json here", "{}", '{"description": "", "tags": []}',
                 '{"tags": [', '{"tags": ["a"'):  # nothing salvageable
        try:
            parse_caption(junk)
        except ValueError:
            pass
        else:
            raise AssertionError(f"should have rejected {junk!r}")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all checks passed")
