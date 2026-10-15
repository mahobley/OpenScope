from pathlib import Path
exec(compile((Path(SPECPATH) / "sonar_head_unit.spec").read_text(), "sonar_head_unit.spec", "exec"))
