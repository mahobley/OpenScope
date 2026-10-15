# Contributing

Open an issue before substantial changes. Include your OS/architecture and
hardware setup when reporting sonar behavior. Keep pull requests focused and
include a description of the behavior changed and relevant validation.
Contributions to this project are submitted under its MIT License; third-party
code must retain its original license and attribution. Dependency changes must
update THIRD_PARTY_NOTICES.md, the licensing reference/source manifests, and
applicable corresponding-source requirements in docs/LICENSING.md.

Use Python 3.12 and Node.js 22.12+. From the repository root:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt pytest
.venv/bin/python -m pytest -q backend/tests
cd desktop
npm ci
npm test
SONAR_PYTHON="$(pwd)/../.venv/bin/python" npm run smoke:backend
```

On Windows use `.venv\Scripts\python.exe` and PowerShell environment syntax.
Source tests do not require sonar hardware. Hardware validation must separately
check live viewing, range and enable/disable controls, recording, shutdown,
and network cleanup. Never claim support for a platform based on mocks alone.

Follow existing Python/JavaScript conventions. Avoid including logs, captures,
recordings, credentials, local environments, or generated build outputs in PRs.
Report security issues privately as described in SECURITY.md.
