"""Client profiles: the replacement for the old per-faculty time exponent."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import yaml
import pricing
from slicefile import FilamentUse, SlicedJob

ROOT = pathlib.Path(__file__).resolve().parents[1]
CFG = {k: yaml.safe_load((ROOT / "config" / f"{k}.yaml").read_text(encoding="utf-8"))
       for k in ("pricing", "printers", "filaments")}


def q(hours, profile=None, grams=100.0, material="PLA", **kw):
    job = SlicedJob([FilamentUse(material, grams)], hours * 3600, plates=1)
    return pricing.build_quote(
        job, pricing=CFG["pricing"], printers=CFG["printers"],
        filaments=CFG["filaments"], printer_id="normal",
        client_profile=profile, **kw)


def test_prep_is_flat_not_duration_scaled():
    """The cost the old exponent never charged: file prep does not grow
    with print length."""
    short = next(l.amount for l in q(0.5, "architecture").lines if l.key == "prep")
    long_ = next(l.amount for l in q(20, "architecture").lines if l.key == "prep")
    assert short == long_ == 25 / 60 * 120, (short, long_)
    print(f"  flat prep       0.5 h and 20 h both bill B{short:.2f} of prep")


def test_architecture_premium_no_longer_inverts():
    """Old scheme: arch/eng premium was hours**0.2, so BELOW one hour an
    architecture student paid LESS. That must not happen now."""
    for h in (0.1, 0.25, 0.5, 0.75):
        e, a = q(h, "engineering").total, q(h, "architecture").total
        old_premium = h ** 0.2
        assert a > e, f"at {h} h architecture ({a}) must exceed engineering ({e})"
        assert old_premium < 1.0
    print("  no inversion    arch > eng at 0.1/0.25/0.5/0.75 h "
          "(old scheme charged arch LESS at all four)")


def test_premium_is_bounded():
    """Old scheme grew without limit (2.17x at 48 h, still climbing).
    The new one must stay in a sane band over the realistic range."""
    prem = {}
    for h in (0.25, 1, 4, 10, 24, 48):
        e, a = q(h, "engineering").total, q(h, "architecture").total
        prem[h] = a / e
    assert all(1.05 < v < 1.45 for v in prem.values()), prem
    print(f"  bounded         premium {min(prem.values()):.2f}x-{max(prem.values()):.2f}x "
          f"across 0.25-48 h (old scheme: {0.25**0.2:.2f}x-{48**0.2:.2f}x)")


def test_risk_cap_engages_on_very_long_prints():
    """Without a cap the exponential model makes a 100 h print absurd."""
    quote = q(100, "engineering")
    r = quote.breakdown["plates"][0]["risk"]
    assert r["factor"] == CFG["pricing"]["max_risk_factor"]
    assert r["factor_uncapped"] > r["factor"]
    assert any("capped" in w for w in quote.warnings)
    print(f"  risk cap        100 h: uncapped x{r['factor_uncapped']:.2f} "
          f"-> capped x{r['factor']:.2f}, warning raised")


def test_risk_compounds_material_and_client():
    """A thin-walled architecture model in ABS is both problems at once."""
    r = q(10, "architecture", material="ABS").breakdown["plates"][0]["risk"]
    assert r["material_risk_multiplier"] == 1.6      # ABS on 'normal'
    assert r["client_risk_multiplier"] == 1.45
    assert abs(r["combined_risk_multiplier"] - 1.6 * 1.45) < 1e-9
    print(f"  compounding     ABS x1.6 * architecture x1.45 "
          f"= x{r['combined_risk_multiplier']}")


def test_prep_override_beats_profile_default():
    """An architecture student with a clean file should not pay the
    architecture prep charge."""
    default = next(l.amount for l in q(5, "architecture").lines if l.key == "prep")
    clean = next(l.amount for l in q(5, "architecture", prep_minutes=5).lines
                 if l.key == "prep")
    assert default == 50.0 and clean == 10.0
    print(f"  override        architecture default B{default:.0f} prep, "
          f"clean file B{clean:.0f}")


def test_postproc_multiplier_applies():
    base = q(5, "engineering", postproc_minutes=30)
    arch = q(5, "architecture", postproc_minutes=30)
    b = next(l.amount for l in base.lines if l.key == "postproc")
    a = next(l.amount for l in arch.lines if l.key == "postproc")
    assert abs(a / b - 1.6) < 1e-9
    print(f"  postproc        30 min bills B{b:.0f} eng / B{a:.0f} arch (x1.6)")


def test_internal_is_free_and_skips_the_floor():
    quote = q(5, "internal")
    assert quote.total == 0.0, quote.total
    print(f"  internal        B{quote.total:.2f}, minimum-price floor bypassed")


def test_unknown_profile_warns_and_falls_back():
    quote = q(5, "law_school")
    assert any("law_school" in w for w in quote.warnings)
    assert quote.total > 0
    print(f"  unknown profile warns and still prices (B{quote.total:.2f})")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"\nRunning {len(fns)} client-profile tests\n")
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed\n")
    sys.exit(1 if failed else 0)
