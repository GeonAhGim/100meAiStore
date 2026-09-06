# Third-party review notice (draft)

The project license and distribution model remain undecided (DEC-06). This
file is an inventory/review notice, not a license grant for repository contents.

| Component | Observed local version | Distribution metadata | Role |
|---|---|---|---|
| setuptools | 80.9.0 | MIT | Build tooling; pyproject requires >=68 |
| bcrypt | 4.3.0 | Apache-2.0 | Optional installed tool used to verify synthetic signing vector |

No upstream source was vendored by Phase 2. Before packaging/distribution, audit
actual resolved build/runtime/transitive components, retain each applicable
license/NOTICE text with the shipped distribution and record the selected
product license. Local metadata is not counsel's approval or a complete SBOM.
Vendor API documentation remains attributed by direct links in evidence files;
synthetic fixtures are authored locally and contain no real customer samples.
