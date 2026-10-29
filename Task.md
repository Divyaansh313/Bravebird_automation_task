Goal
Build a Playwright script that logs into a real portal through a second f actor , reuses the
session on later runs, navigates to a nested form, and saves a document the portal
produces.
A single-process command-line program is the right shape. Infrastructure around it is
extra credit, not a requirement.
The scenario
Use a public site where you hold your own account and the login needs a second f actor
(email code, SMS code, or an authenticator app). Only your own account, only your own
data. If a site blocks automation, pick another . T wo sites are fine if one cannot cover
everything; say so in the README.
The script must:
1. Log in with a second factor, obtaining the code without a human typing it.
2. Reuse the session on the next run, and f all back to a full login when the saved state is
stale.
3. Navigate at least three pages deep to a form with three or more fields, one dependent
on another .
4. Capture a document through a native download, a new tab or window , or print to PDF .
5. Write a JSON manifest: inputs, pages visited, stage timings, captured files.
Inputs come from a JSON file. A different order , date, or search term must need no code
change.
Good picks: e-commerce or food delivery (O TP login, order invoice), telecom or utility
(PDF statements), GitHub with T O TP enabled (release assets).
Core problems to solve
W orking code for each. W e will run it.
Page 1 of 3
Bravebird T ake Home Assignment - Automation
1. Second factor. A code provider behind async def get
_
code(challenge) -> str
with one real implementation: T O TP from a secret, an IMAP poll, or an SMS-forwarding
inbox. Retry a wrong code once. Never log the code.
2. Session persistence. Save state after login, load it next run, detect a dead session by
more than a redirect to the login page, and log in again without help.
3. Resilient locators. No step depends on one CSS or XP ath string. A resolver tries role
and name, label, nearby text, structure, then id or class, and logs which strategy won. It
must run offline against saved HTML.
4. Portal quirks. Handle at least two: an iframe, a new window or tab, a download event,
print to PDF , a dependent field.
5. Retry and evidence. Bounded retries with backoff , retryable f ailures separated from
final ones. On a final f ailure write a screenshot, the page HTML, the URL, the console
log, the stage key , and the resolver's attempts. Redact credentials, codes, and account
identifiers.
How we evaluate
W e do Run with no saved state Run again Corrupt the saved state and run Change the input JSON Mutate your saved HTML and run the
resolver offline
Break a step W e expect
Full login through your provider , document
on disk, manifest complete
No login page, same document, f aster
Dead session detected, full login, success
A different document, no code change
Fields still found after renamed classes,
reordered siblings, an added wrapper div
and iframe
Evidence bundle written, nothing sensitive
in it
The README must cover: how the code arrives and what happens when it is wrong; how a
dead session is detected; the locator strategy order and why; which quirks you handled;
which f ailures retry; what you would change first for a hundred portals.
Page 2 of 3
Bravebird T ake Home Assignment - Automation
Extra credit
Optional, and only once everything above passes: a job queue or API in front of the runner ,
proxy routing, running several portals at once, fingerprint hardening, CAPT CHA handling,
a small UI. Any of these done well earns extra credit.
Submission
Please provide:
Code: A link to a GitHub repository with a one-command setup, one successful run
output, and one f ailure bundle.
README: How to run it, the environment variables it needs, and the answers listed
under How we evaluate.