# Security reporting

OpenScope is beta software. Security fixes target the current release; no
long-term support commitment has been established.

Report vulnerabilities privately through GitHub:
[Report a vulnerability](https://github.com/mahobley/OpenScope/security/advisories/new).
Include the affected version, reproduction steps, and potential impact.
Do not post credentials, private packet captures, or exploit details in public issues.
If private reporting is unavailable, open a minimal issue asking for a private
reporting channel without sharing sensitive details.

OpenScope serves local interfaces and needs administrator rights for sonar
networking. Keep the controller bound to loopback and use a dedicated sonar
adapter. Review exported diagnostics before sharing. macOS and Linux elevate
only the Python backend; Windows runs the application with administrator rights.
See [known issues](docs/KNOWN_ISSUES.md).
