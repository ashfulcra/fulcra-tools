def test_public_surface_importable():
    from fulcra_okf import (
        OKF_VERSION, OKFError, Concept, Bundle, validate, Report, Finding,
        FrontmatterError, ext,
    )
    assert OKF_VERSION == "0.1"
    assert callable(validate)
    assert ext.NAMESPACE == "x_fulcra_"
    b = Bundle()
    b.concepts["a"] = Concept(id="a", type="T")
    assert validate(b).conformant is True
