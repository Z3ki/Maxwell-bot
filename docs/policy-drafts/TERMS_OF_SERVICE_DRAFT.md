# Terms of service draft — operator review required

**Status: DRAFT. Do not publish or treat this as legal advice.** This is a
reviewable starting point based on the current source. Operator identity,
jurisdiction, provider terms, consumer-law requirements, and live feature
configuration have not been verified. Obtain operator and legal review before
replacing `web/terms/index.html`.

**Draft date:** 2026-09-28

**Service:** Maxwell hosted Discord application and related websites/APIs

**Operator identity/contact:** `[operator must complete]`

## 1. Scope and agreement

These terms will cover the Maxwell service operated by `[operator]`, including
the hosted Discord application and related websites/APIs identified at
`[verified service URLs]`. They will not govern copies of the MIT-licensed
software that other people host themselves. The published terms must identify
the actual operator and provide a valid contact method.

## 2. Experimental service

The hosted service is experimental and may change, be unavailable, or lose
data. It is not a substitute for professional advice, emergency services, or a
backup. The operator must confirm this description matches the deployed
service.

## 3. Features and third parties

Depending on configuration, Maxwell may generate responses, use memory, search
the web, process attachments, create or publish sites, use voice or media
features, and run isolated shell jobs. Shell is available only when the host's
required gVisor runtime and external egress controls pass readiness checks; it
fails closed when they are missing. The operator must confirm which features
are enabled on the hosted instance before publication.

Discord and selected AI, image, search, speech, hosting, and infrastructure
providers may process data needed to provide a feature. Their terms and privacy
policies also apply. The privacy policy must identify the actual configured
providers and the information each feature sends.

## 4. User content and acceptable use

People retain rights they already hold in content they submit. They authorize
the operator to process and transmit that content only as needed to provide the
requested service, maintain security, and operate features described in the
privacy policy. Generated sites and content explicitly published to a public
URL may be visible to anyone.

People may not use the service to violate law or platform rules, attack or
overload the service or third parties, access another person's private data,
extract credentials, or publish content the operator is not permitted to host.
The operator must ensure these rules match its moderation and enforcement
practices.

## 5. Limits and paid access

The source currently disables premium billing. Do not advertise a paid plan,
price, quota, or entitlement unless it has been implemented and the full terms
are shown before purchase. Any service limits may change only as allowed by
applicable law and the final published policy.

## 6. Availability, suspension, and liability

The final terms must state the service's actual warranty, suspension,
termination, refund, and liability rules in language reviewed for the
operator's jurisdiction. Do not publish the prior site's fixed liability cap
or other legal language without confirming it is valid and intended.

## 7. Governing law and contact

`[Operator must supply jurisdiction, dispute process, and contact details after
legal review.]`

## Publication checklist

- [ ] Verify operator identity, hosted URLs, enabled features, provider list,
  and privacy disclosures.
- [ ] Verify shell is either unavailable or passes the disposable-host
  containment test before describing it as active.
- [ ] Confirm billing and quota status against the deployed application.
- [ ] Review age eligibility, content license, acceptable-use, termination,
  consumer disclosures, governing law, and liability language with counsel.
- [ ] Update the website only after operator approval; add a version and
  effective date.
