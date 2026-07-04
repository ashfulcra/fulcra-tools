def test_script_loads_and_declares_contract(ni):
    assert ni.DEF_NAME == "Watched"
    assert ni.DEF_MARKER == "com.fulcradynamics.annotation.media.watched"
    assert ni.API_BASE.startswith("https://")
