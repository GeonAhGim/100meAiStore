# P2-04 L4: synthetic signing vectors and bounded read retry plans

Status: implementing offline subset. Naver sources: [authentication](https://apicenter.commerce.naver.com/docs/auth),
[restrictions](https://apicenter.commerce.naver.com/docs/restriction). Coupang
source: [official test guide](https://developers.coupang.com/ko/getting-started/open-api-test-guide).
No new vendor API or credential request is permitted.

Only fixed public/synthetic signing inputs are accepted; no application-key
parameters or Authorization header factory is exposed. Naver uses an injected
bcrypt hash function to avoid adding a runtime dependency to the core; verify
the official public vector against installed bcrypt 4.3.0 separately. CI tests
check the signing-input/base64 boundary. Coupang uses a synthetic fixed key and
UTC timestamp + GET path + the same encoded query from the offline request plan.

Retry planning is pure and bounded by attempt count, deadline and at most one
fixture reauthentication. Only GET read plans qualify. Auth-code/status pairs,
rate vs quota headers and unknown errors remain distinct; missing/malformed
limits or unknown quota periods require manual review. Local exponential delays
are client policy, not vendor reset-time claims. No plan sleeps or calls a URL.

P2-03 already supplies durable opaque/page-sequence checkpoint evidence. Add
tests for encoded cursor preservation, UTC conversion, malformed headers,
deadline/retry exhaustion and no-secret repr. Do not represent fixture signatures
as working authentication. Requested-by header spelling remains inconsistent in
the official guide; no transport uses it, and resolution remains an open P2-04
item until authoritative confirmation. Continue other offline slices meanwhile.

## Observed official-example incompatibility

Installed bcrypt 4.3.0 rejected the public example salt with `Invalid salt`.
The API now converts this into fixed `public_fixture_bcrypt_incompatible`
without retaining the backend message. Do not mark the public vector verified
or substitute a corrected salt into the official example silently.

A separately named local canonical synthetic vector succeeds with bcrypt 4.3.0.
Its low work factor is exclusively for fixtures and is not a production-secret
recommendation. Unit tests verify input/base64 behavior through an injected
function; the actual installed bcrypt calculation is a separate local gate.
P2-04 stays in progress because public-example compatibility and the Coupang
requested-by spelling need authoritative resolution before transport work.

Offline subset evidence: seven targeted tests and full suite 151 passed;
compileall/diff/secret-pattern/forbidden-filename checks passed. The separate
canonical synthetic vector passed against installed bcrypt 4.3.0. No HTTP
transport, key parameters or new runtime dependencies were added. Next safe
work: P2-06 settlement/ledger fixture mapping while these source issues remain.
