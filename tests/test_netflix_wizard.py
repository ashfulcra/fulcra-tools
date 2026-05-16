from click.testing import CliRunner

from fulcra_media.wizards.netflix import walkthrough


def test_walkthrough_slim_route():
    runner = CliRunner()
    # input "1" picks the slim CSV route
    result = runner.invoke(walkthrough, input="1\n")
    assert result.exit_code == 0
    assert "Viewing activity" in result.output
    assert "Download all" in result.output
    assert "netflix.com/account" in result.output
    assert "M/D/YY" in result.output  # warn about precision


def test_walkthrough_gdpr_route():
    runner = CliRunner()
    result = runner.invoke(walkthrough, input="2\n")
    assert result.exit_code == 0
    assert "netflix.com/account/getmyinfo" in result.output
    assert "up to 30 days" in result.output
    assert "10 columns" in result.output or "rich" in result.output.lower()
    # New: confirm rich import is wired (no more "not yet wired up" disclaimer)
    assert "not yet wired" not in result.output
    assert "fulcra-media import netflix" in result.output


def test_walkthrough_rejects_bad_choice():
    runner = CliRunner()
    result = runner.invoke(walkthrough, input="9\n")
    # Click's Choice will keep prompting; abort by EOF
    assert result.exit_code != 0 or "Invalid" in result.output
