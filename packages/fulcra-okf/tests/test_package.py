def test_version_and_base_error():
    import fulcra_okf
    assert fulcra_okf.OKF_VERSION == "0.1"
    assert issubclass(fulcra_okf.OKFError, Exception)
