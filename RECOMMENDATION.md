# Messaging Gateway Recommendation

## Decision

Do not abandon Keybase, but do not submit the current implementation as an
in-tree Hermes gateway platform.

Treat Keybase as an experimental, external integration for a specific Linux
service account. Its security model remains appealing: public-key identity,
cryptographic verification, and encrypted conversations are real strengths.
The practical problem is reach and operational shape. Most people do not
already use Keybase, and its verification model is harder to explain and use
than a phone-number or familiar account-based messenger.

The current Keybase draft should be preserved as research, then rebuilt only
if a Linux deployment proves the actual service model. It should begin as a
standalone Hermes platform plugin, not a core contribution.

## Why The Current Draft Is Not Ready

Hermes profiles scope Hermes-owned data through `HERMES_HOME`: configuration,
sessions, memory, skills, logs, cron state, and gateway state. They do not
normally isolate external CLI state: host-side subprocesses retain the real
Unix `HOME` by default.

Keybase is different from a normal token-based gateway. The local CLI talks to
a locally logged-in Keybase service. That identity is effectively owned by a
Unix user and its Keybase service state. It is not naturally one account per
Hermes profile.

The supported operating model should therefore be:

> One Keybase daemon and one Keybase account per Unix service identity; only
> one Hermes gateway process may own that identity at a time.

A token lock can prevent accidental concurrent use, but it cannot create
profile-level Keybase account isolation. Do not promise that it can.

The draft also has two divergent adapter copies:

- `gateway/platforms/keybase.py`
- `plugins/platforms/keybase/__init__.py`

That must be resolved before any functional discussion. The binary package and
host-specific Compose override are deployment artifacts, not contribution
source.

## Recommended Keybase Path

1. Keep the branch as a research branch. Do not add more core wiring.
2. Test on the Ubuntu host, not the macOS development machine.
3. Create a dedicated non-root Unix account, such as `hermes-keybase`.
4. Run Keybase and the one Hermes gateway process under that same account.
5. Use an explicit Keybase home only after proving that two independent Linux
   Keybase service/account instances can coexist. Until then, regard it as a
   singleton service.
6. Verify receive, send, attachment handling, restart recovery, duplicate
   suppression, `/clear` semantics, and behavior when another Hermes profile
   is running.
7. If that works, publish it as a standalone platform plugin with installation
   and systemd documentation. It can later be proposed upstream with real
   Linux evidence and a clear single-instance contract.

Never run an internet-facing agent gateway as root merely to accommodate a
messaging client. Put any privileged deployment work outside the agent process
and give the gateway only the paths and permissions it requires.

## Alternatives Worth Using With Hermes

Choose the channel based on the people you need to reach, not on an abstract
ranking of protocols. Every channel must still use Hermes allowlists or DM
pairing: a secure transport does not make agent tool access safe by itself.

| Option | Best when | What it preserves or improves | Main tradeoff |
|---|---|---|---|
| **Signal** | Privacy-first 1:1 and small trusted groups | Mainstream E2EE, strong user expectations around privacy, free, and Hermes already supports it through `signal-cli` | Still has a local daemon/account lifecycle and phone-number identity; less suitable for broad public/community adoption |
| **Matrix** | You value federation, self-hosting, room ownership, and open infrastructure | Closest strategic successor to Keybase's independence and crypto ethos; Hermes supports rooms, threads, files, reactions, and optional/required E2EE | Federation and E2EE bot/device management are operationally demanding; user reach is smaller than Telegram/Discord/WhatsApp |
| **Telegram** | You need the lowest-friction mainstream bot channel | Large reach, polished bot UX, groups, topics, media, voice, files, and streaming; Hermes support is mature | Bot chats are not end-to-end encrypted; Telegram's trust model is not Keybase's PKI model |
| **Discord** | Your people already live in technical or community servers | Strong community mechanics, threads, roles, reactions, media, voice, and excellent Hermes gateway support | Not a privacy-first network; requires bot permissions and intentional server access design |
| **Slack** | The agent belongs in an existing workplace | Modern Socket Mode does not require a public inbound endpoint; strong threads and enterprise workflow fit | Workspace-bound, proprietary, and typically paid at organizational scale |
| **WhatsApp Business Cloud API** | You need ordinary consumer reach, particularly phone-centric audiences | Enormous existing user base; official API is the stable production path | Meta account/business setup and webhook infrastructure; E2EE does not mean the bot integration has Keybase-style end-to-end control |
| **Email** | Universality, auditability, and asynchronous work matter more than chat immediacy | Everyone has it; standard IMAP/SMTP; no client onboarding; Hermes supports threaded replies and attachments | Slow interaction loop and a larger phishing/prompt-injection surface; use a dedicated inbox and explicit allowlist |
| **SimpleX** | You want to explore privacy-preserving, identifier-minimizing messaging | A credible up-and-coming privacy option; Hermes has a bundled platform plugin | Smaller ecosystem and less operational maturity than Signal or Matrix; validate the plugin and service model before relying on it |
| **ntfy** | You primarily need notifications, not a conversational home | Lightweight, self-hostable, easy to adopt, and available as a Hermes plugin | It is a notification transport, not a rich conversation platform |

## Practical Recommendation

Use a two-channel strategy instead of forcing one network to satisfy every
need:

- **Primary personal/private channel:** Signal.
- **Open, self-hosted, or community-controlled channel:** Matrix.
- **Reach and collaboration channel:** Telegram for a small public/private
  audience, Discord for an existing community, or Slack for a workplace.
- **Universal fallback and audit trail:** a dedicated Hermes email account.
- **Keep Keybase:** as an opt-in personal channel for people who already use
  it or are specifically willing to adopt its trust model.

For the current situation, the most useful near-term comparison is **Signal
versus Matrix**. Signal is the easy answer when privacy and ordinary human
adoption matter. Matrix is the serious answer when self-hosting, federation,
and open infrastructure matter. Telegram or Discord should be added only when
reach outweighs their weaker privacy posture.

## Contribution Boundary

Hermes documents platform adapters as a plugin-first surface. A Keybase
integration belongs outside the core repository until it has a demonstrated
Linux deployment, a documented account/service boundary, and users beyond the
original operator.

A future proposal should include:

- a standalone plugin repository;
- Linux/systemd installation instructions for a non-root service account;
- a clear statement that one Keybase service identity is owned by one gateway;
- end-to-end tests against a disposable `HERMES_HOME` where feasible;
- a compatibility matrix for the supported Keybase CLI/service versions; and
- no bundled `.deb`, account state, or site-specific Compose override.
