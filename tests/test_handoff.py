from datetime import date

import config
from handoff import handoff_command, parse_legs


def test_handoff_command_round_trips_legs():
    legs = [("ILG", "TPA", date(2026, 10, 9)), ("LAL", "ILG", date(2026, 10, 12))]
    cmd = handoff_command(legs, promo="save20", python="py")
    assert cmd == ["py", str(config.PROJECT_ROOT / "handoff.py"), "--legs", "ILG-TPA:2026-10-09,LAL-ILG:2026-10-12", "--promo", "SAVE20"]
    assert parse_legs(cmd[3]) == legs
    assert "--promo" not in handoff_command(legs, python="py")
