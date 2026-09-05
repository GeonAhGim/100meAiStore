# Implementation records

This directory records implementation decisions and acceptance evidence. The
normative design remains in [`../architecture`](../architecture/README.md).

Detailed task contracts and the next implementation sequence are in
[`L4-development-notes.md`](L4-development-notes.md). CORE-01 is the first
implementation packet; its ten acceptance checks are implementation targets,
not a claim that they have already passed.

Phase 1 is limited to a local DEMO foundation and the first control-plane
vertical slice. It does not authorize live marketplace, supplier, payment, or
cloud side effects.

Phase 2 persistence and failure-injection acceptance criteria are tracked in
[`phase2-test-plan.md`](phase2-test-plan.md). Passing a smaller unit-test subset
does not imply that the complete Phase 2 release gate has passed.
