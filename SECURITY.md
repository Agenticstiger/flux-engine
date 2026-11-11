# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | yes |

Before 1.0, only the latest minor release gets fixes.

## Reporting a vulnerability

Please do not report a security problem in a public issue or pull request.

Use GitHub's private vulnerability reporting: open this repository's
**Security** tab and choose **Report a vulnerability**
([direct link](https://github.com/Agenticstiger/flux-engine/security/advisories/new)).
The report stays between you and the maintainers until a fix is out. It is
also where we draft the advisory and request a CVE when one is warranted.

If you cannot use that form, open an issue with the **Ask for a private
security channel** form. It asks you to leave out every detail; a maintainer
will answer there and set up a private channel.

It helps to include:

- what the problem is and what an attacker gains from it;
- the affected version, or the commit you tested;
- a minimal bundle, contract or command line that reproduces it;
- any configuration the reproduction needs.

Examples of what we would like to hear about: a bundle, contract or state
directory that makes the engine read or write outside the directories it was
given, a way to forge or alter an evidence pack's digests or signature, or a
flaw in how a release is built or verified.

## What to expect

- We acknowledge a report within 3 working days.
- Within 14 days we tell you whether we can reproduce it and how we plan to
  fix it.
- We tell you when the fix is released, and credit you in the release notes
  unless you prefer to stay anonymous.

We follow
[coordinated disclosure](https://en.wikipedia.org/wiki/Coordinated_vulnerability_disclosure).
Please give us reasonable time to fix the problem before you publish, avoid
privacy violations and service disruption while you test, and do not touch
other people's data.

Problems in a third-party dependency belong with that project. If the engine
uses the dependency in a way that makes the problem worse, tell us.
