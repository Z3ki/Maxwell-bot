# Privacy policy — superseded note

Last updated: 2026-11-01

**Before launch, the operator must fill in their identity and contact details.**
The published privacy policy contains neutral placeholders exactly because this
checkout must never invent a company name, address, jurisdiction, or
registration number. Do not publish regional or compliance claims (for example
GDPR transfer safeguards or provider data-use settings) until they have been
verified for the real deployment.

The canonical, published Privacy Policy is:

- **`web/privacy/index.html`** (this repository)

This file used to hold a working draft with an open provider-verification
checklist. It has been retired so there is only one version of the truth: the
published page above. If the published policy and any old copy of this draft
disagree, the published page wins.

Current implementation defaults missing message-history preferences to on;
explicit off choices stop storage and delete that account's history. Changing the default does not itself delete old history.
Existing retention and explicit deletion controls still apply. Provider
processing of live requests is separate from optional local history.

The published policy also describes operational diagnostics, aggregate metrics,
estimated activity, short-lived opt-in membership hashes, retention and the
limits of deletion from aggregates and external logs.

What the operator still has to decide or verify (nothing below is legal
advice):

- [ ] Operator identity and a monitored contact route (the published policy
      says "the operator of this Maxwell instance" on purpose — the real
      identity must be filled in before launch).
- [ ] Inventory the live primary, fallback, vision, embedding, image,
      speech, and search endpoints actually configured on the hosted instance,
      without exposing credentials.
- [ ] For each provider account, confirm the legal entity behind the service,
      region, retention, and data-use/training settings before strengthening
      any claim about what providers do with content.
- [ ] Confirm actual storage, backup, and deletion behaviour on the host. The
      published policy describes self-service history controls and supported
      full deletion, while workspaces, backups and external copies have
      separate limitations and may require private support.
- [ ] Whether a dedicated privacy contact or data-protection officer is
      required for the operator's jurisdiction.

Once those are settled, edit `web/privacy/index.html` directly and update its
"Last updated" date. Keep this note short — do not reintroduce a second full
draft of the policy here.
