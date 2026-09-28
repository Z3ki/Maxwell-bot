# Privacy policy draft — operator review required

**Status: DRAFT. Do not publish or treat this as legal advice.** This draft is
based on the code in this checkout. The deployed provider accounts, their
settings, the operator's legal identity, and applicable legal requirements have
not been verified. Complete the checklist at the end and obtain operator and
legal review before replacing `web/privacy/index.html`.

**Draft date:** 2026-09-28

**Service:** Maxwell hosted Discord application and related websites/APIs

**Operator identity/contact:** `[operator must complete]`

## Scope and service

This policy will describe the hosted Maxwell service operated by the project.
It will not govern self-hosted installations. The hosted service may process
Discord interactions, messages and attachments, memory, generated content,
published sites, and optional tools. A feature is available only when the
operator has enabled and configured its dependencies.

## Information processed

Depending on a person's use of the service, Maxwell may process:

- Discord user, server, channel, message, and interaction identifiers and
  display names;
- message text, selected recent context, attachments, and content submitted to
  tools;
- user preferences, private reply context, memory, and user-configured provider
  credentials;
- generated text, images, files, and site content that a person asks Maxwell to
  create or publish;
- operational records such as timestamps, request metadata, tool activity, and
  error information.

BYOK credentials are encrypted at rest and associated with the Discord user
ID and selected provider. They are not intended to be included in prompts or
logs. The operator must verify the deployed encryption-key and backup
procedures before making this a public assurance.

## Uses and disclosures

Information is used to operate requested features, maintain context and
memory, protect the service, enforce limits, and diagnose failures. Discord
receives Discord API payloads. Content needed for an AI, embedding, image,
speech, search, or other tool request may be sent to the provider selected by
the operator or the user. BYOK requests use one of the fixed OpenAI, OpenRouter,
or Groq endpoints in the code. The provider receives the context included in
that request, which may include prompts, authorized history, attachments, and
tool results.

**Provider retention, training, region, subprocessors, and account settings are
not yet verified.** Do not state that a provider does not retain or train on
content until the actual service, account, endpoint, features, and controls have
been confirmed. Third-party services process data under their own terms.

## Storage and retention

Application data is stored in the operator-configured data directory and
related databases. Memory, preferences, encrypted credentials, logs, generated
sites, and backups may have different retention periods. The final policy must
name the deployed storage and document actual retention and deletion behavior;
those values are not verified in this draft.

Shell workspaces, when the host's required gVisor runtime and external network
policy are provisioned, are container-local and ephemeral. Cancellation,
expiry, or reset destroys the guest and its workspace. Shell remains
unavailable when those host prerequisites are missing. This behavior does not
describe site code or other application storage.

## Choices and requests

People may stop using Maxwell and may request deletion of data associated with
their Discord ID through `[operator-approved contact or command]`. The operator
must verify which memory stores, generated content, provider records, and
backups can be deleted, and explain any limits before publishing this section.

## International processing and children

Discord, the host, and selected providers may process data in other countries.
The operator must verify provider regions and any transfer safeguards before
making a regional or compliance statement. The service's age rules must be
confirmed against Discord requirements and applicable law.

## Security and changes

The operator uses access controls and service-specific safeguards, but no
internet service can promise absolute security. Policy changes will be dated
and posted before they take effect where required by law.

## Provider and operator verification checklist

- [ ] Inventory the live primary, fallback, vision, embedding, image, speech,
  search, and BYOK endpoints without exposing credentials.
- [ ] For each account, record the legal service entity, account/project, model,
  service tier, region, data-use/training setting, retention setting, and
  enabled API features.
- [ ] Review current official terms and any account-specific contract or data
  processing addendum. Confirm settings in the provider console or with support.
- [ ] Confirm what is sent by each feature, including attachments, memory,
  tool results, and user-install interactions.
- [ ] Confirm local database/log/site retention, backup retention, deletion
  paths, access controls, and the process for user requests.
- [ ] Fill in the operator's legal identity, contact, jurisdiction, age rules,
  and international transfer details.
- [ ] Obtain operator and legal review, then update the published page and
  effective date.

Provider references to verify at publication time:

- [OpenAI API data controls](https://developers.openai.com/api/docs/guides/your-data)
- [OpenRouter provider logging](https://openrouter.ai/docs/guides/privacy/provider-logging)
- [GroqCloud data controls](https://console.groq.com/docs/your-data)
