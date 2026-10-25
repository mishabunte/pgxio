# Security policy

pgxio is an experimental source preview. There are no supported production
versions or promised security maintenance windows yet. Do not treat historical
local test results as an independent security audit.

Please report suspected vulnerabilities through
[GitHub private vulnerability reporting](https://github.com/mishabunte/pgxio/security/advisories/new).
Include a minimal reproduction, affected revision, expected boundary and observed
behavior. Do not include live credentials or customer data. Please avoid public
issues containing exploit details before the report has been assessed.

The runtime's authentication, SQL permissions, notification and availability
boundaries are documented in [docs/security.md](docs/security.md).
