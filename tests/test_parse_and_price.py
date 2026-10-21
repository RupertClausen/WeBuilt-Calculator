"""Smoke tests: parse each fixture, price it, assert the model behaves."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import yaml
import pricing
from slicefile import parse, ParseError

ROOT = pathlib.Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures"
CFG = {k: yaml.safe_load((ROOT / "config" / f"{k}.yaml").read_text())
       for k in ("pricing", "printers", "filaments")}


def price(name, **kw):
    """parse() returns one job PER PLATE; price them as one order."""
    jobs = parse(name, (FX / name).read_bytes())
    q = pricing.build_quote(jobs, pricing=CFG["pricing"], printers=CFG["printers"],
                            filaments=CFG["filaments"], **kw)
    return jobs, q


def one(name, **kw):
    jobs, q = price(name, **kw)
    assert len(jobs) == 1, f"{name} has {len(jobs)} plates"
    return jobs[0], q


def test_orca_gcode():
    job, q = one("orca_petg_single.gcode")
    assert [f.type for f in job.filaments] == ["PETG"]
    assert abs(job.total_grams - 222.86) < 0.01
    assert abs(job.hours - 9.8167) < 0.01
    assert q.total > 0
    print(f"  orca gcode      {job.total_grams:>7.2f} g {job.hours:>6.2f} h -> B{q.total:,.2f}")


def test_3mf_matches_gcode():
    """Same job via slice_info.config must price identically to the gcode."""
    _, q_gcode = one("orca_petg_single.gcode")
    job3, q3 = one("bambu_petg.gcode.3mf")
    assert job3.source.startswith("3mf")
    assert abs(job3.total_grams - 222.86) < 0.01
    assert abs(q3.total - q_gcode.total) < 0.01, (q3.total, q_gcode.total)
    print(f"  3mf == gcode    both -> B{q3.total:,.2f}")


def test_multi_material():
    job, q = one("bambu_ams_multi.gcode")
    types = sorted(f.type for f in job.filaments)
    assert types == ["PETG", "PLA"], types
    assert abs(job.total_grams - 60.95) < 0.01
    # The riskier of the two materials must drive the risk factor
    assert q.breakdown["plates"][0]["risk"]["material_risk_multiplier"] == 1.2  # PETG on 'normal'
    print(f"  ams multi       {types} {job.total_grams:.2f} g -> B{q.total:,.2f}")


def test_tiny_job_is_not_underpriced():
    """The B8 orders in the WeBuilt sheet are the problem the setup fee fixes.
    A 3 g keychain must clear the minimum on its own merits."""
    job, q = one("orca_tiny_pla.gcode")
    assert q.total >= CFG["pricing"]["minimum_price"]
    setup = next(l.amount for l in q.lines if l.key == "setup")
    assert setup == CFG["pricing"]["setup_fee"] == 50
    print(f"  tiny job        {job.total_grams:.2f} g {job.hours*60:.0f} min "
          f"-> B{q.total:,.2f} (setup B{setup:.0f} carries it)")


def test_minimum_price_floor_engages():
    """With fees stripped out, the floor is what stops a near-zero quote."""
    import copy
    cfg = copy.deepcopy(CFG["pricing"])
    cfg["setup_fee"] = 0
    cfg["additional_plate_fee"] = 0
    jobs = parse("orca_tiny_pla.gcode", (FX / "orca_tiny_pla.gcode").read_bytes())
    q = pricing.build_quote(jobs, pricing=cfg, printers=CFG["printers"],
                            filaments=CFG["filaments"], printer_id="normal")
    assert q.total == cfg["minimum_price"], q.total
    assert any("minimum" in w.lower() for w in q.warnings), q.warnings
    print(f"  floor backstop  fees stripped -> B{q.total:,.2f} (floor applied)")


def test_unsliced_3mf_rejected():
    try:
        parse("unsliced_project.3mf", (FX / "unsliced_project.3mf").read_bytes())
    except ParseError as e:
        assert "not a sliced one" in str(e)
        print(f"  unsliced 3mf    rejected: {str(e)[:48]}...")
        return
    raise AssertionError("should have raised")


def test_per_printer_failure_model():
    """The same job on two machines must get different risk, not just a
    different hourly rate."""
    _, qn = one("orca_petg_single.gcode", printer_id="normal")
    _, ql = one("orca_petg_single.gcode", printer_id="large")
    rn = qn.breakdown["plates"][0]["risk"]
    rl = ql.breakdown["plates"][0]["risk"]
    assert rn["model"] == "exponential" and rl["model"] == "weibull"
    assert rn["shape"] == 1.0 and rl["shape"] == 1.3
    assert rl["p_success_first_try"] < rn["p_success_first_try"]
    assert rl["factor"] > rn["factor"]
    print(f"  normal  p={rn['p_success_first_try']:.4f} risk x{rn['factor']:.3f} "
          f"shape {rn['shape']} -> B{qn.total:,.2f}")
    print(f"  large   p={rl['p_success_first_try']:.4f} risk x{rl['factor']:.3f} "
          f"shape {rl['shape']} -> B{ql.total:,.2f}")


def test_material_risk_is_per_printer():
    """ABS must be penalised more on 'large' than on 'normal' because each
    machine carries its own material_risk table."""
    n = CFG["printers"]["printers"]["normal"]
    l = CFG["printers"]["printers"]["large"]
    rn, _ = pricing.printer_material_risk(n, "ABS", CFG["filaments"])
    rl, _ = pricing.printer_material_risk(l, "ABS", CFG["filaments"])
    assert rn == 1.6 and rl == 2.1, (rn, rl)
    # and an unlisted material falls back to the fleet default
    rf, basis = pricing.printer_material_risk(n, "ASA", CFG["filaments"])
    assert rf == 1.9 and basis == "fleet default"
    print(f"  ABS risk        normal x{rn}  large x{rl}   (ASA falls back x{rf})")


def test_no_time_exponent():
    """Doubling print time must NOT more than double machine time."""
    import copy
    from slicefile import SlicedJob, FilamentUse
    mk = lambda h: SlicedJob([FilamentUse("PLA", 100.0)], h * 3600, plates=1)
    kw = dict(pricing=CFG["pricing"], printers=CFG["printers"],
              filaments=CFG["filaments"], printer_id="normal")
    q1 = pricing.build_quote(mk(1), **kw)
    q2 = pricing.build_quote(mk(2), **kw)
    m1 = next(l.amount for l in q1.lines if l.key == "machine")
    m2 = next(l.amount for l in q2.lines if l.key == "machine")
    assert abs(m2 - 2 * m1) < 1e-6, (m1, m2)
    print(f"  linear time     1h machine B{m1:.2f}, 2h machine B{m2:.2f} (exactly 2x)")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"\nRunning {len(fns)} tests\n")
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed\n")
    sys.exit(1 if failed else 0)
